"""图像采集与发送工具（gpt-image-2.5绘画 · 冻结接口 v1.0.0）。

本模块负责三件事：

1. 采集输入图片：同消息附件、文本中的图片链接、引用消息图片、@ 用户头像；
2. 把各种来源（http(s) / data URL / base64 / 本地文件）统一解码为 ``ImageInput``；
3. 把模型返回的图片转成 AstrBot 消息组件并发送回当前会话。

设计约定：

* 采集类函数「尽力而为」：单张图片失败只记录日志，绝不向上抛异常；
* 只有 :func:`fetch_image` 会对非法输入抛出 ``ProtocolError``，
  由调用方决定是忽略还是提示用户；
* 模块顶层的 AstrBot 导入均做了容错处理，脱离 AstrBot 环境也能 ``py_compile`` 与单测。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import tempfile
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

try:  # pragma: no cover - 正常 AstrBot 环境必然可导入
    import astrbot.api.message_components as Comp
except Exception:  # noqa: BLE001 - 允许脱离 AstrBot 环境做单测 / 语法检查
    Comp = None  # type: ignore[assignment]

try:  # pragma: no cover - 正常 AstrBot 环境必然可导入
    from astrbot.api.message_components import File as _CompFile
except Exception:  # noqa: BLE001 - 允许脱离 AstrBot 环境做单测 / 语法检查
    _CompFile = None  # type: ignore[assignment]

try:  # pragma: no cover - 正常 AstrBot 环境必然可导入
    from astrbot.api import logger
except Exception:  # noqa: BLE001 - 退化到标准库日志，保证模块可用
    logger = logging.getLogger("astrbot_plugin_gpt_image")

from .models import GeneratedImage, GenerateResult, ImageInput, ProtocolError

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from astrbot.api.event import AstrMessageEvent

    from .config import PluginConfig

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

MAX_IMAGE_BYTES: int = 20 * 1024 * 1024
"""单张图片体积上限（20MB），本地文件与网络下载共用。"""

DEFAULT_FETCH_TIMEOUT: float = 60.0
"""单张图片下载默认超时（秒）。"""

AVATAR_FETCH_TIMEOUT: float = 20.0
"""@ 用户头像下载超时（秒）：头像体积小，超时更短。"""

QQ_AVATAR_URL_TEMPLATE: str = "https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"
"""OneBot v11 用户头像接口（640px）。"""

_BROWSER_UA: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 链接字符集：排除空白、中文标点/汉字、全角符号、尖括号与引号，避免把后文一起吞掉
_URL_PATTERN = re.compile(
    r"https?://[^\s\u3000-\u303f\u4e00-\u9fff\uff00-\uffef"
    r"\u2018\u2019\u201c\u201d\u2013\u2014\u2026\u00b7<>\"'`]+",
    re.IGNORECASE,
)

_TRAILING_PUNCTUATION: str = "，。！？、；：,.!?;:）】》」』”’…·*#'\"`|"

_IMAGE_SUFFIXES: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".svg"},
)

_IMAGE_PATH_HINTS: tuple[str, ...] = (
    "image",
    "img",
    "photo",
    "pic",
    "qlogo",
    "multimedia",
    "gchat.qpic",
    "c2cpic",
)

_IMAGE_HOST_HINTS: tuple[str, ...] = (
    "qpic.cn",
    "qlogo.cn",
    "gtimg.cn",
    "gchat.qpic",
    "c2cpic",
    "multimedia.nt.qq.com.cn",
    "img.qq.com",
)

_MIME_BY_SUFFIX: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".svg": "image/svg+xml",
}

_SUFFIX_BY_MIME: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/svg+xml": ".svg",
}

_BARE_BASE64_PATTERN = re.compile(r"^[A-Za-z0-9+/=\s]{64,}$")


# --------------------------------------------------------------------------- #
# 组件与配置访问辅助
# --------------------------------------------------------------------------- #
def _comp_class(name: str) -> Any:
    """返回 ``astrbot.api.message_components`` 里的组件类，不可用时返回 ``None``。"""
    if Comp is None:
        return None
    return getattr(Comp, name, None)


def _is_component(obj: Any, name: str) -> bool:
    """安全判断 ``obj`` 是否为指定消息组件（组件模块缺失时返回 ``False``）。"""
    cls = _comp_class(name)
    if cls is None:
        return False
    try:
        return isinstance(obj, cls)
    except Exception:  # noqa: BLE001 - 极端情况下组件类不可用于 isinstance
        return False


def _iter_messages(event: Any) -> list[Any]:
    """安全获取事件的消息组件列表。"""
    getter = getattr(event, "get_messages", None)
    if not callable(getter):
        return []
    try:
        messages = getter()
    except Exception:  # noqa: BLE001
        return []
    if not messages:
        return []
    try:
        return list(messages)
    except Exception:  # noqa: BLE001
        return []


def _cfg_value(cfg: Any, name: str, default: Any) -> Any:
    """读取配置项：既支持 ``PluginConfig`` 的方法，也支持普通属性 / 字典。"""
    if cfg is None:
        return default
    value = getattr(cfg, name, None)
    if value is None:
        if isinstance(cfg, dict):
            return cfg.get(name, default)
        return default
    if callable(value):
        try:
            return value()
        except Exception:  # noqa: BLE001
            return default
    return value


def _cfg_flag(cfg: Any, name: str, default: bool) -> bool:
    """读取布尔配置项。"""
    raw = _cfg_value(cfg, name, default)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on", "是"}
    return bool(raw)


def _cfg_int(cfg: Any, name: str, default: int) -> int:
    """读取整数配置项。"""
    try:
        return int(_cfg_value(cfg, name, default))
    except (TypeError, ValueError):
        return default

# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def _fingerprint(data: bytes) -> str:
    """返回图片内容的去重指纹（sha1 前 16 位）。"""
    return hashlib.sha1(data or b"").hexdigest()[:16]


def _sanitize_filename(name: str) -> str:
    """清洗文件名，去掉路径分隔符与非法字符（兼容 Windows）。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(name or "").strip())
    cleaned = cleaned.strip(". ")
    if not cleaned:
        return "image.png"
    stem, ext = os.path.splitext(cleaned)
    if len(stem) > 64:
        cleaned = stem[:64] + ext
    return cleaned


def _guess_mime_from_name(name: str) -> str:
    """按扩展名猜测图片 MIME，无法识别时返回空串。"""
    _, ext = os.path.splitext(str(name or "").lower())
    return _MIME_BY_SUFFIX.get(ext, "")


def _normalize_mime(mime: str, filename: str = "") -> str:
    """规范化 MIME，并在缺失时按文件名补全。"""
    value = (mime or "").split(";")[0].strip().lower()
    if value.startswith("image/jpg"):
        value = "image/jpeg"
    if not value or not value.startswith("image/"):
        value = _guess_mime_from_name(filename) or "image/png"
    return value


def _normalize_filename(filename: str, mime: str) -> str:
    """确保文件名带扩展名（按 MIME 补全）。"""
    name = _sanitize_filename(filename or "image.png")
    _, ext = os.path.splitext(name)
    if ext:
        return name
    suffix = _SUFFIX_BY_MIME.get(_normalize_mime(mime), ".png")
    return "{}{}".format(os.path.splitext(name)[0] or "image", suffix)


def _filename_from_url(url: str, default: str = "image.png") -> str:
    """从 URL 路径里推断文件名。"""
    try:
        path = unquote(urlsplit(url).path or "")
    except Exception:  # noqa: BLE001
        path = ""
    name = os.path.basename(path)
    if not name or "." not in name:
        return default
    return name


def _sniff_mime(data: bytes) -> str:
    """按文件头魔数识别图片类型，无法识别时返回空串。"""
    if not data:
        return ""
    head = data[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    return ""


def _file_uri_to_path(value: str) -> str:
    """把 ``file://`` URI 转成本地路径（兼容 Windows 盘符）。"""
    try:
        parsed = urlsplit(value)
    except Exception:  # noqa: BLE001
        return value
    path = url2pathname(parsed.path or "")
    netloc = parsed.netloc or ""
    if netloc and netloc.lower() != "localhost":
        path = url2pathname("//{}{}".format(netloc, parsed.path or ""))
    if len(path) > 2 and path[0] in "\\/" and path[1].isalpha() and path[2] == ":":
        path = path[1:]
    return path


def _strip_url_tail(raw: str) -> str:
    """剥离 URL 结尾的标点（中文标点、右括号、右中括号等）。"""
    url = raw
    safety = len(url) + 8
    while url and safety > 0:
        safety -= 1
        changed = False
        for opener, closer in (("(", ")"), ("[", "]"), ("{", "}")):
            if url.endswith(closer) and url.count(closer) > url.count(opener):
                url = url[:-1]
                changed = True
                break
        if not changed and url and url[-1] in _TRAILING_PUNCTUATION:
            url = url[:-1]
            changed = True
        if not changed:
            break
    return url


def looks_like_image_url(url: str) -> bool:
    """判断链接是否「看起来是图片」（后缀、路径关键词或图床域名）。"""
    value = (url or "").strip()
    if not value:
        return False
    lowered = value.lower()
    if lowered.startswith(("data:image/", "base64://")):
        return True
    try:
        split = urlsplit(value)
    except Exception:  # noqa: BLE001
        return False
    if not split.scheme and not split.netloc:
        return False
    path = unquote(split.path or "")
    _, ext = os.path.splitext(path.lower())
    if ext in _IMAGE_SUFFIXES:
        return True
    host = (split.netloc or "").lower()
    if any(hint in host for hint in _IMAGE_HOST_HINTS):
        return True
    haystack = "{}{}".format(host, path).lower()
    if any(hint in haystack for hint in _IMAGE_PATH_HINTS):
        return True
    query = (split.query or "").lower()
    return any(hint in query for hint in ("image", "img=", "photo", "pic="))


def _build_image_input(
    data: bytes,
    *,
    source: str,
    filename: str = "",
    mime_hint: str = "",
) -> ImageInput:
    """根据原始字节构造 ``ImageInput``（自动嗅探 MIME）。"""
    sniffed = _sniff_mime(data)
    name = _normalize_filename(filename or "image.png", mime_hint or sniffed)
    mime = sniffed or _normalize_mime(mime_hint, name)
    return ImageInput(
        data=data,
        mime=mime,
        filename=_normalize_filename(name, mime),
        source=source,
    )


# --------------------------------------------------------------------------- #
# 公开接口：URL 解析
# --------------------------------------------------------------------------- #
async def extract_image_urls(text: str) -> tuple:
    """从文本中解析链接，并把**图片链接**从文本里剥离。

    参数:
        text: 待解析的原始文本（通常是去掉指令名之后的用户输入）。

    返回:
        ``(剥离图片链接后的文本, 所有链接列表)``。第二个元素按出现顺序返回
        **全部**链接（图片链接与普通链接都包含）；文本只剔除图片链接，
        普通链接会原样保留，方便中转站 / 提示词里携带参考地址。
    """
    raw_text = text or ""
    if not raw_text.strip():
        return raw_text, []

    links: list[str] = []
    spans: list[tuple[int, int, bool]] = []
    for match in _URL_PATTERN.finditer(raw_text):
        candidate = _strip_url_tail(match.group(0))
        if not candidate:
            continue
        start = match.start()
        links.append(candidate)
        spans.append((start, start + len(candidate), looks_like_image_url(candidate)))

    if not links:
        return raw_text, []

    image_spans = [span for span in spans if span[2]]
    if not image_spans:
        return raw_text, links

    pieces: list[str] = []
    cursor = 0
    for start, end, _ in image_spans:
        if start < cursor:
            continue
        pieces.append(raw_text[cursor:start])
        pieces.append(" ")
        cursor = end
    pieces.append(raw_text[cursor:])

    cleaned = re.sub(r"[ \t\u3000]{2,}", " ", "".join(pieces))
    cleaned = re.sub(r"[ \t\u3000]+(\r?\n)", r"\1", cleaned)
    cleaned = re.sub(r"(\r?\n)[ \t\u3000]+", r"\1", cleaned)
    return cleaned.strip(), links


# --------------------------------------------------------------------------- #
# 公开接口：图片下载 / 解码
# --------------------------------------------------------------------------- #
async def fetch_image(url: str, timeout: float = DEFAULT_FETCH_TIMEOUT) -> ImageInput:
    """把任意图片引用读取为 ``ImageInput``。

    支持的形式：``http(s)://``、``data:image/...;base64,``、``base64://``、
    裸 base64 字符串、``file://`` URI 以及本地绝对路径。

    参数:
        url: 图片引用。
        timeout: 网络下载超时（秒），默认 60 秒。

    返回:
        解码完成的 ``ImageInput``。

    异常:
        ProtocolError: 引用为空、下载失败、内容不是图片或超过 20MB 上限。
    """
    value = str(url or "").strip()
    if not value:
        raise ProtocolError("图片地址为空")

    lowered = value.lower()
    if lowered.startswith("data:image/"):
        return _fetch_data_url(value)
    if lowered.startswith("data:"):
        raise ProtocolError("仅支持 data:image/ 开头的 data URL", detail=value[:64])
    if lowered.startswith("base64://"):
        return _fetch_base64(value[len("base64://"):], source="base64")
    if lowered.startswith("file://"):
        return _fetch_local_path(_file_uri_to_path(value), source=value)
    if lowered.startswith(("http://", "https://")):
        return await _fetch_http(value, timeout=timeout)
    if _looks_like_local_path(value):
        return _fetch_local_path(value, source=value)
    if _BARE_BASE64_PATTERN.match(value):
        return _fetch_base64(value, source="base64")

    raise ProtocolError(
        "不支持的图片地址（仅支持 http(s)、data URL、base64、file:// 与本地路径）",
        detail=value[:128],
    )


def _looks_like_local_path(value: str) -> bool:
    """判断字符串是否像本地路径。"""
    if not value:
        return False
    try:
        if os.path.isabs(value):
            return True
        return os.path.exists(value)
    except Exception:  # noqa: BLE001 - 非法路径字符
        return False


def _fetch_data_url(value: str) -> ImageInput:
    """解析 ``data:image/...;base64,xxxx`` 形式的图片。"""
    try:
        image = ImageInput.from_base64(value, source="data-url")
    except Exception as exc:  # noqa: BLE001
        raise ProtocolError("data URL 解码失败：{}".format(exc), detail=value[:64]) from exc
    if not image.data:
        raise ProtocolError("data URL 内容为空", detail=value[:64])
    if len(image.data) > MAX_IMAGE_BYTES:
        raise ProtocolError("图片体积超过 20MB 上限")
    image.mime = _normalize_mime(image.mime, image.filename)
    image.filename = _normalize_filename(image.filename, image.mime)
    return image


def _fetch_base64(payload: str, *, source: str) -> ImageInput:
    """解码裸 base64 / ``base64://`` 内容。"""
    try:
        image = ImageInput.from_base64(payload, source=source)
    except Exception as exc:  # noqa: BLE001
        raise ProtocolError("base64 图片解码失败：{}".format(exc)) from exc
    if not image.data:
        raise ProtocolError("base64 图片内容为空")
    if len(image.data) > MAX_IMAGE_BYTES:
        raise ProtocolError("图片体积超过 20MB 上限")
    image.mime = _normalize_mime(image.mime, image.filename)
    image.filename = _normalize_filename(image.filename, image.mime)
    return image


def _fetch_local_path(path: str, *, source: str) -> ImageInput:
    """读取本地图片文件。"""
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ProtocolError(
            "读取本地图片失败：{}".format(exc), detail=str(path)[:128],
        ) from exc
    if size <= 0:
        raise ProtocolError("本地图片内容为空", detail=str(path)[:128])
    if size > MAX_IMAGE_BYTES:
        raise ProtocolError("图片体积超过 20MB 上限", detail=str(path)[:128])
    try:
        with open(path, "rb") as fp:
            data = fp.read()
    except OSError as exc:
        raise ProtocolError(
            "读取本地图片失败：{}".format(exc), detail=str(path)[:128],
        ) from exc

    filename = os.path.basename(path) or "image.png"
    if not _sniff_mime(data) and not _guess_mime_from_name(filename):
        raise ProtocolError("本地文件不是图片", detail=str(path)[:128])
    return _build_image_input(data, source=source, filename=filename)


async def _fetch_http(url: str, timeout: float) -> ImageInput:
    """通过 aiohttp 下载网络图片并校验内容。"""
    import aiohttp  # 局部导入：aiohttp 随 AstrBot 安装，避免模块级硬依赖

    headers = {"User-Agent": _BROWSER_UA, "Accept": "image/*,*/*"}
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    chunks: list[bytes] = []
    total = 0
    content_type = ""
    try:
        async with aiohttp.ClientSession(
            timeout=client_timeout, trust_env=True,
        ) as session:
            async with session.get(url, headers=headers, allow_redirects=True) as resp:
                if resp.status >= 400:
                    raise ProtocolError(
                        "下载图片失败：HTTP {}".format(resp.status),
                        status=resp.status,
                        detail=url[:128],
                    )
                content_type = (resp.headers.get("Content-Type") or "")
                content_type = content_type.split(";")[0].strip().lower()
                async for chunk in resp.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        raise ProtocolError("图片体积超过 20MB 上限", detail=url[:128])
                    chunks.append(chunk)
    except ProtocolError:
        raise
    except asyncio.TimeoutError as exc:
        raise ProtocolError(
            "下载图片超时（{:.0f} 秒）".format(timeout), detail=url[:128],
        ) from exc
    except aiohttp.ClientError as exc:
        raise ProtocolError("下载图片失败：{}".format(exc), detail=url[:128]) from exc
    except Exception as exc:  # noqa: BLE001 - 兜底，统一转成协议异常
        raise ProtocolError("下载图片失败：{}".format(exc), detail=url[:128]) from exc

    data = b"".join(chunks)
    if not data:
        raise ProtocolError("图片内容为空", detail=url[:128])
    sniffed = _sniff_mime(data)
    if not content_type.startswith("image/") and not sniffed:
        raise ProtocolError(
            "链接返回的内容不是图片",
            detail="{} ({})".format(url[:96], content_type or "unknown"),
        )

    filename = _filename_from_url(url) or "image.png"
    return _build_image_input(
        data, source=url, filename=filename, mime_hint=sniffed or content_type,
    )

# --------------------------------------------------------------------------- #
# 内部工具：组件 -> ImageInput
# --------------------------------------------------------------------------- #
async def _component_to_image_input(comp: Any, source: str) -> "ImageInput | None":
    """把消息组件里的图片转成 ``ImageInput``，失败返回 ``None``。

    依次尝试：``convert_to_base64()`` → ``convert_to_file_path()`` → ``url``/``file`` 字段。
    QQ 官方适配器的 ``Image`` 可能只有 ``file``/``url``，因此第三种兜底是必要的。
    """
    errors: list[str] = []

    converter = getattr(comp, "convert_to_base64", None)
    if callable(converter):
        try:
            payload = await converter()
            if payload:
                image = ImageInput.from_base64(str(payload), source=source)
                if image.data and len(image.data) <= MAX_IMAGE_BYTES:
                    return image
        except Exception as exc:  # noqa: BLE001
            errors.append("convert_to_base64: {}".format(exc))

    path_converter = getattr(comp, "convert_to_file_path", None)
    if callable(path_converter):
        try:
            path = await path_converter()
            if path:
                return _fetch_local_path(str(path), source=source)
        except Exception as exc:  # noqa: BLE001
            errors.append("convert_to_file_path: {}".format(exc))

    for attr in ("url", "file", "path"):
        value = getattr(comp, attr, None)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            return await fetch_image(value.strip(), timeout=DEFAULT_FETCH_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            errors.append("{}: {}".format(attr, exc))

    if errors:
        logger.warning("图片组件解析失败（source=%s）：%s", source, "；".join(errors[:3]))
    return None


async def _ref_to_image_input(ref: Any, source: str) -> "ImageInput | None":
    """把引用消息里的图片引用（URL / base64 / 本地路径）转成 ``ImageInput``。"""
    if isinstance(ref, ImageInput):
        return ref if ref.data else None
    if not isinstance(ref, str) or not ref.strip():
        return None
    try:
        return await fetch_image(ref.strip())
    except Exception as exc:  # noqa: BLE001
        logger.warning("引用图片解析失败（source=%s）：%s", source, exc)
        return None


async def _gather_limited(factories: list[Any], limit: int = 4) -> list[Any]:
    """并发执行协程工厂，最多 ``limit`` 个并发，保持返回顺序（异常转 ``None``）。"""
    if not factories:
        return []
    semaphore = asyncio.Semaphore(max(1, int(limit)))

    async def _run(factory: Any) -> Any:
        async with semaphore:
            try:
                return await factory()
            except Exception as exc:  # noqa: BLE001
                logger.warning("并发采集图片失败：%s", exc)
                return None

    return await asyncio.gather(*(_run(factory) for factory in factories))


def _dedupe_images(images: list["ImageInput | None"]) -> list["ImageInput"]:
    """按内容指纹去重，保持顺序。"""
    result: list[ImageInput] = []
    seen: set[str] = set()
    for image in images:
        if image is None or not image.data:
            continue
        key = _fingerprint(image.data)
        if key in seen:
            continue
        seen.add(key)
        result.append(image)
    return result


# --------------------------------------------------------------------------- #
# 公开接口：采集
# --------------------------------------------------------------------------- #
async def collect_attachment_images(event: Any) -> list[ImageInput]:
    """采集当前消息里附带的图片组件。"""
    components = [
        message for message in _iter_messages(event) if _is_component(message, "Image")
    ]
    if not components:
        return []

    factories = [
        (lambda comp=comp: _component_to_image_input(comp, "attachment"))
        for comp in components
    ]
    return _dedupe_images(await _gather_limited(factories))


async def collect_url_images(text: str) -> list[ImageInput]:
    """采集文本中的图片链接（普通链接会保留在文本里，由调用方处理）。"""
    _, links = await extract_image_urls(text or "")
    targets = [link for link in links if looks_like_image_url(link)]
    if not targets:
        return []

    factories = [(lambda link=link: _ref_to_image_input(link, "url")) for link in targets]
    return _dedupe_images(await _gather_limited(factories))


class ReplyImageList(list):
    """既是 ``list`` 又是可等待对象，兼容同步与异步两种调用姿势。

    * 直接当列表用：返回随事件下发的引用图片（base64 / 本地文件，无需联网）；
    * ``await`` 使用：返回完整结果，包含 ``extract_quoted_message_images``
      与 OneBot ``get_msg`` 解析出的远程图片。
    """

    def __init__(self, items: list[ImageInput], coro_factory: Any) -> None:
        super().__init__(items)
        self._coro_factory = coro_factory

    def __await__(self):
        return self._coro_factory().__await__()


def extract_reply_images(event: Any) -> list[ImageInput]:
    """提取引用消息中的图片。

    返回 :class:`ReplyImageList`——既可直接当列表读取（同步、离线部分），
    也可 ``await`` 获得完整结果（优先 ``extract_quoted_message_images``，
    其次遍历 ``Comp.Reply.chain`` 组件，最后尝试 OneBot ``get_msg`` 补救）。
    """
    sync_items = _extract_reply_images_sync(event)
    return ReplyImageList(
        sync_items,
        lambda: extract_reply_images_async(event),
    )


def _extract_reply_images_sync(event: Any) -> list[ImageInput]:
    """同步实现：只解析已经随事件下发的引用内容（不发起远程调用）。"""
    images: list[ImageInput] = []
    for message in _iter_messages(event):
        if not _is_component(message, "Reply"):
            continue
        chain = getattr(message, "chain", None) or []
        for item in chain:
            if not _is_component(item, "Image"):
                continue
            for attr in ("url", "file", "path"):
                value = getattr(item, attr, None)
                if not isinstance(value, str) or not value.strip():
                    continue
                payload = _decode_reference_sync(value)
                if payload is not None:
                    payload.source = "reply"
                    images.append(payload)
                    break
    return _dedupe_images(images)


def _decode_reference_sync(value: str) -> "ImageInput | None":
    """同步解析引用里的图片引用（仅支持 base64 / 本地文件 / data URL）。"""
    text = (value or "").strip()
    if not text:
        return None
    lowered = text.lower()
    try:
        if lowered.startswith("base64://"):
            return _fetch_base64(text[len("base64://"):], source="reply")
        if lowered.startswith("data:image/"):
            return _fetch_data_url(text)
        if lowered.startswith("file://"):
            return _fetch_local_path(_file_uri_to_path(text), source="reply")
        if _looks_like_local_path(text):
            return _fetch_local_path(text, source="reply")
    except Exception as exc:  # noqa: BLE001
        logger.warning("引用图片同步解析失败：%s", exc)
    return None


async def extract_reply_images_async(event: Any) -> list[ImageInput]:
    """异步提取引用消息图片（含 OneBot 远程兜底）。"""
    refs: list[Any] = []
    try:
        from astrbot.core.utils.quoted_message_parser import (
            extract_quoted_message_images,
        )
    except Exception:  # noqa: BLE001 - 旧版本没有该工具，走回退逻辑
        extract_quoted_message_images = None  # type: ignore[assignment]

    if extract_quoted_message_images is not None:
        try:
            refs = list(await extract_quoted_message_images(event) or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("解析引用消息图片失败：%s", exc)
            refs = []

    images = _dedupe_images(
        await _gather_limited(
            [(lambda ref=ref: _ref_to_image_input(ref, "reply")) for ref in refs],
        ),
    )
    if images:
        return images

    chain_images = await _collect_reply_chain_images(event)
    if chain_images:
        return chain_images

    images = extract_reply_images(event)
    if images:
        return images

    fetched = await _fetch_reply_images_via_onebot(event)
    return _dedupe_images(fetched)


async def _collect_reply_chain_images(event: Any) -> list[ImageInput]:
    """遍历 ``Comp.Reply.chain`` 里的图片组件逐个解码（支持 base64 / URL / 本地文件）。"""
    components: list[Any] = []
    for message in _iter_messages(event):
        if not _is_component(message, "Reply"):
            continue
        for item in getattr(message, "chain", None) or []:
            if _is_component(item, "Image"):
                components.append(item)
    if not components:
        return []

    factories = [
        (lambda comp=comp: _component_to_image_input(comp, "reply"))
        for comp in components
    ]
    return _dedupe_images(await _gather_limited(factories))


async def _fetch_reply_images_via_onebot(event: Any) -> list[ImageInput]:
    """兜底：对 ``aiocqhttp`` 平台用 ``get_msg`` 拉取被引用消息里的图片。"""
    platform = ""
    try:
        platform = str(event.get_platform_name() or "")
    except Exception:  # noqa: BLE001
        platform = ""
    if platform and platform != "aiocqhttp":
        return []

    bot = getattr(event, "bot", None)
    api = getattr(bot, "api", None)
    call_action = getattr(api, "call_action", None)
    if not callable(call_action):
        call_action = getattr(bot, "call_action", None)
    if not callable(call_action):
        return []

    reply_ids: list[str] = []
    for message in _iter_messages(event):
        if not _is_component(message, "Reply"):
            continue
        reply_id = getattr(message, "id", None)
        if reply_id is None:
            continue
        text = str(reply_id).strip()
        if text:
            reply_ids.append(text)
    if not reply_ids:
        return []

    images: list[ImageInput] = []
    for reply_id in reply_ids:
        payload: Any = None
        for params in ({"message_id": reply_id}, {"message_id": int(reply_id) if reply_id.isdigit() else reply_id}):
            try:
                payload = await call_action("get_msg", **params)
                break
            except Exception:  # noqa: BLE001 - 可选增强，失败必须静默
                payload = None
        if not isinstance(payload, dict):
            continue
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        segments = data.get("message") if isinstance(data, dict) else None
        if not isinstance(segments, list):
            continue
        refs: list[str] = []
        for segment in segments:
            if not isinstance(segment, dict) or str(segment.get("type")) != "image":
                continue
            info = segment.get("data")
            if not isinstance(info, dict):
                continue
            for key in ("url", "file"):
                value = info.get(key)
                if isinstance(value, str) and value.strip():
                    refs.append(value.strip())
        images.extend(
            await _gather_limited(
                [(lambda ref=ref: _ref_to_image_input(ref, "reply")) for ref in refs],
            ),
        )
    return images


async def collect_mention_images(event: Any, cfg: Any = None) -> list[ImageInput]:
    """采集被 @ 用户的头像（OneBot v11）。

    QQ 官方适配器的 ``At`` 只有 openid / ``qq_official`` 占位，没有真实 QQ 号，
    这里会直接跳过。
    """
    self_id = ""
    try:
        self_id = str(event.get_self_id() or "")
    except Exception:  # noqa: BLE001
        self_id = ""

    qqs: list[str] = []
    for message in _iter_messages(event):
        if not _is_component(message, "At"):
            continue
        raw_qq = getattr(message, "qq", None)
        if raw_qq is None:
            continue
        qq = str(raw_qq).strip()
        if not qq or qq == "all" or not qq.isdigit():
            continue
        if self_id and qq == self_id:
            continue
        if qq not in qqs:
            qqs.append(qq)
    if not qqs:
        return []

    factories = [
        (
            lambda qq=qq: _ref_to_image_input(
                QQ_AVATAR_URL_TEMPLATE.format(qq=qq), "avatar",
            )
        )
        for qq in qqs
    ]
    images = await _gather_limited(factories)
    for image in images:
        if image is not None:
            image.source = "avatar"
    return _dedupe_images(images)


async def collect_input_images(
    event: Any,
    cfg: Any,
    *,
    include_mentions: bool = False,
) -> list[ImageInput]:
    """按优先级收集本次请求的全部输入图片。

    顺序为：同消息附件图片 → 文本中的图片链接 → 引用消息图片（需开启）
    → ``include_mentions=True`` 时的 @ 用户头像。最后按 ``cfg.max_input_images()``
    截断，只保留前 N 张。单张图片失败不会影响其他图片。
    """
    images: list[ImageInput] = []

    for collector in (
        lambda: collect_attachment_images(event),
        lambda: collect_url_images(_safe_message_str(event)),
    ):
        try:
            images.extend(await collector())
        except Exception as exc:  # noqa: BLE001
            logger.warning("采集输入图片失败：%s", exc)

    if _cfg_flag(cfg, "reply_reference_image", True):
        try:
            images.extend(await extract_reply_images_async(event))
        except Exception as exc:  # noqa: BLE001
            logger.warning("采集引用图片失败：%s", exc)

    if include_mentions:
        try:
            images.extend(await collect_mention_images(event, cfg))
        except Exception as exc:  # noqa: BLE001
            logger.warning("采集 @ 头像失败：%s", exc)

    deduped = _dedupe_images(images)
    limit = _cfg_int(cfg, "max_input_images", 6)
    if limit > 0 and len(deduped) > limit:
        deduped = deduped[:limit]
    return deduped


def _safe_message_str(event: Any) -> str:
    """安全获取事件纯文本。"""
    getter = getattr(event, "get_message_str", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:  # noqa: BLE001
            return ""
    return ""



# --------------------------------------------------------------------------- #
# 发送降级辅助（需求 4 / 6：整链 → 逐条 → 文件 → 链接 → 文本）
# --------------------------------------------------------------------------- #

_UPLOAD_LIMIT_KEYWORDS = (
    "too large",
    "too big",
    "oversize",
    "oversized",
    "exceed",
    "exceeds",
    "file size",
    "max size",
    "maximum size",
    "payload too large",
    "413",
    "400",
    "retcode",
    "size",
    "limit",
    "upload",
    "过大",
    "太大",
    "超出",
    "超限",
    "体积",
    "上传失败",
)
"""错误文本命中这些关键字时，判为「体积 / 上传受限」（忽略大小写）。"""

_TEMP_IMAGE_DIR_NAME = "astrbot_gpt_image_out"
"""降级为文件发送时使用的临时目录名（位于系统临时目录下）。"""

_TEMP_IMAGE_KEEP = 200
"""临时目录内最多保留的图片文件数量，超出后从最旧的开始清理。"""

_COMPRESS_MAX_BYTES = 1500 * 1024
"""图片发送失败后尝试压缩的目标体积（1.5MB），仍超限则继续降级。"""

_COMPRESS_MAX_EDGE = 2048
"""压缩时允许的最大边长（像素），超过则等比缩放。"""

try:  # pragma: no cover - Pillow 是 AstrBot 的依赖，此处仅做可选容错
    from PIL import Image as _PILImage
except Exception:  # noqa: BLE001 - 没装 Pillow 时跳过压缩环节
    _PILImage = None  # type: ignore[assignment]


def _compress_image(
    data: Any, filename: str = "image.png",
) -> tuple:
    """把图片压到 :data:`_COMPRESS_MAX_BYTES` 以内，返回 ``(数据, 文件名)``。

    Pillow 不可用、数据为空或压缩失败时返回空元组；如果怎么压都达不到目标
    体积，则返回「尽力压缩」后的最小结果（只要比原图明显更小）。压缩结果
    统一为 JPEG，因此文件名后缀会被改写；只有「原样发送」失败后才会走到这里。
    """
    if _PILImage is None or not isinstance(data, (bytes, bytearray)) or not data:
        return ()
    payload = bytes(data)
    if len(payload) <= _COMPRESS_MAX_BYTES:
        return ()
    try:
        import io

        with _PILImage.open(io.BytesIO(payload)) as source:
            image = source.convert("RGB")
            width, height = image.size
            longest = max(width, height)
            if longest > _COMPRESS_MAX_EDGE:
                scale = _COMPRESS_MAX_EDGE / float(longest)
                # 不同 Pillow 版本的重采样常量名不同，逐个兜底
                resample = getattr(
                    _PILImage,
                    "LANCZOS",
                    getattr(_PILImage, "ANTIALIAS", getattr(_PILImage, "BICUBIC", 1)),
                )
                image = image.resize(
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    resample,
                )
            stem = os.path.splitext(_sanitize_filename(filename or "image"))[0] or "image"
            name = "{}.jpg".format(stem)
            best = b""
            for quality in (88, 80, 72, 64, 56, 48):
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=quality, optimize=True)
                compressed = buffer.getvalue()
                if not best or len(compressed) < len(best):
                    best = compressed
                if len(compressed) <= _COMPRESS_MAX_BYTES:
                    image.close()
                    return compressed, name
            image.close()
            # 达不到目标体积时：只要明显变小（至少省 10%）就仍然值得一试
            if best and len(best) <= len(payload) * 9 // 10:
                logger.info(
                    "图片压缩后仍超过目标体积：%d -> %d 字节", len(payload), len(best),
                )
                return best, name
    except Exception as exc:  # noqa: BLE001 - 压缩失败不阻断后续降级
        logger.warning("压缩图片失败，继续其它降级方式：%s", exc)
    return ()


def _compressed_component(component: Any, image: Any = None) -> Any:
    """尝试把图片压缩后重新构造组件。

    成功返回 ``(组件, GeneratedImage)`` 元组；Pillow 不可用、缺少原始数据
    或压缩失败时返回 ``None``。
    """
    data = getattr(image, "data", None)
    if not isinstance(data, (bytes, bytearray)) or not data:
        return None
    result = _compress_image(data, _output_filename(component, image))
    if not result:
        return None
    compressed, _ = result
    try:
        compressed_image = GeneratedImage(data=compressed, mime="image/jpeg")
        compressed_component = image_to_component(compressed_image)
    except Exception as exc:  # noqa: BLE001
        logger.warning("构造压缩后图片组件失败：%s", exc)
        return None
    if compressed_component is None:
        return None
    return compressed_component, compressed_image


def _looks_like_upload_limit(text: Any) -> bool:
    """判断错误文本是否像「体积过大 / 上传受限」，用于决定是否改发文件。

    命中任意关键字（忽略大小写）即返回 ``True``；任何异常都视为 ``False``。
    """
    try:
        value = str(text or "").lower()
    except Exception:  # noqa: BLE001 - 极端对象转字符串失败
        return False
    if not value:
        return False
    for keyword in _UPLOAD_LIMIT_KEYWORDS:
        if keyword in value:
            return True
    return False


def _platform_name(event: Any) -> str:
    """安全读取平台名（如 ``aiocqhttp`` / ``qq_official``）。"""
    getter = getattr(event, "get_platform_name", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:  # noqa: BLE001
            return ""
    meta = getattr(event, "platform_meta", None)
    return str(getattr(meta, "name", "") or "") if meta is not None else ""


def _is_qq_official(event: Any) -> bool:
    """是否为 QQ 官方机器人平台（websocket / webhook 均以 ``qq_official`` 开头）。"""
    return _platform_name(event).strip().lower().startswith("qq_official")


def _file_component_class() -> Any:
    """返回 ``Comp.File`` 组件类，不可用时返回 ``None``。"""
    cls = _comp_class("File")
    if cls is not None:
        return cls
    return _CompFile


def _component_url(component: Any) -> str:
    """从图片组件里取出 http(s) 直链（没有则返回空串）。"""
    for attr in ("url", "file_", "file"):
        value = getattr(component, attr, None)
        if isinstance(value, str) and value.strip().lower().startswith(("http://", "https://")):
            return value.strip()
    return ""


def _image_url(image: Any) -> str:
    """从 ``GeneratedImage`` / ``ImageInput`` 里取出 http(s) 直链。"""
    if image is None:
        return ""
    for attr in ("url", "file"):
        value = getattr(image, attr, None)
        if isinstance(value, str) and value.strip().lower().startswith(("http://", "https://")):
            return value.strip()
    return ""


def _existing_local_path(value: Any) -> str:
    """把 ``file://`` / 路径字符串统一成本地绝对路径；文件不存在返回空串。"""
    if not isinstance(value, str) or not value.strip():
        return ""
    candidate = value.strip()
    if candidate.lower().startswith("file://"):
        candidate = _file_uri_to_path(candidate)
    try:
        if candidate and os.path.exists(candidate):
            return os.path.abspath(candidate)
    except Exception:  # noqa: BLE001 - 非法路径字符
        return ""
    return ""


def _output_filename(component: Any, image: Any = None) -> str:
    """为「以文件形式发送」的图片挑一个安全文件名。"""
    candidates = []
    if image is not None:
        candidates.append(getattr(image, "filename", None))
    candidates.append(getattr(component, "name", None))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            mime = getattr(image, "mime", "") if image is not None else ""
            return _normalize_filename(candidate.strip(), str(mime or ""))
    link = _component_url(component) or _image_url(image)
    if link:
        return _normalize_filename(_filename_from_url(link, "image.png"), "")
    return "image.png"


def _prune_temp_dir(directory: str, keep: int = _TEMP_IMAGE_KEEP) -> None:
    """限制临时目录内的文件数量，避免长期运行堆积占用磁盘。"""
    try:
        entries = [
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if not name.startswith(".tmp_")
        ]
    except OSError:
        return
    if len(entries) <= keep:
        return
    try:
        entries.sort(key=lambda item: os.path.getmtime(item))
    except OSError:
        return
    for path in entries[: max(0, len(entries) - keep)]:
        try:
            os.unlink(path)
        except OSError:
            continue


def _write_temp_file(data: bytes, filename: str) -> str:
    """把图片数据原子写入临时目录，返回绝对路径；失败返回空串。"""
    if not data:
        return ""
    try:
        directory = os.path.join(tempfile.gettempdir(), _TEMP_IMAGE_DIR_NAME)
        os.makedirs(directory, exist_ok=True)
        suffix = os.path.splitext(filename or "")[1]
        if not suffix:
            suffix = _SUFFIX_BY_MIME.get(_sniff_mime(data), ".png")
        stem = _sanitize_filename(os.path.splitext(filename or "image")[0] or "image")
        stem = os.path.splitext(stem)[0] or "image"
        target = os.path.join(directory, "{}_{}{}".format(stem, _fingerprint(data), suffix))
        try:
            if os.path.exists(target) and os.path.getsize(target) == len(data):
                return target
        except OSError:
            pass
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=suffix, dir=directory)
        try:
            with os.fdopen(fd, "wb") as fp:
                fp.write(data)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp_path, target)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            raise
        _prune_temp_dir(directory)
        return target
    except Exception as exc:  # noqa: BLE001
        logger.warning("写入临时图片文件失败：%s", exc)
        return ""


def _materialize_local_path(component: Any, image: Any = None) -> str:
    """尽量把图片落地成本地文件路径，供 ``Comp.File`` 使用；失败返回空串。"""
    for candidate in (
        getattr(component, "path", None),
        getattr(component, "file_", None),
        getattr(component, "file", None),
    ):
        path = _existing_local_path(candidate)
        if path:
            return path
    data = getattr(image, "data", None)
    if isinstance(data, (bytes, bytearray)) and len(data) > 0:
        payload = bytes(data)
        name = _output_filename(component, image)
        # 压缩后的字节可能是 JPEG，但文件名仍带 .png，这里按魔数校正扩展名
        sniffed = _sniff_mime(payload)
        if sniffed and _guess_mime_from_name(name) != sniffed:
            stem = os.path.splitext(name)[0] or "image"
            name = _normalize_filename(stem, sniffed)
        return _write_temp_file(payload, name)
    return ""


def _build_file_component(
    file_cls: Any, name: str, file_value: str = "", url_value: str = "",
) -> Any:
    """容错构造 ``Comp.File`` 组件（兼容不同 AstrBot 版本的参数差异）。"""
    source = file_value or url_value
    attempts = (
        lambda: file_cls(name=name, file=file_value, url=url_value),
        lambda: file_cls(name=name, file=source),
        lambda: file_cls(name=name, url=url_value or source),
        lambda: file_cls(name, source),
    )
    for factory in attempts:
        try:
            component = factory()
        except TypeError:
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("构造文件组件失败：%s", exc)
            continue
        if component is not None:
            return component
    return None


async def _send_plain(event: Any, text: Any) -> bool:
    """发送纯文本，成功返回 ``True``；失败只记日志，不抛异常。"""
    value = str(text or "").strip()
    if not value:
        return False
    sender = getattr(event, "plain_result", None)
    if callable(sender):
        try:
            await event.send(sender(value))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("发送纯文本失败：%s", exc)
    plain_cls = _comp_class("Plain")
    if plain_cls is not None:
        try:
            await event.send(event.chain_result([plain_cls(value)]))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("发送文本组件失败：%s", exc)
    return False


async def _send_as_file(event: Any, component: Any, image: Any = None) -> bool:
    """降级方案一：以「文件」形式发送图片（QQ 图片超限时最稳）。"""
    file_cls = _file_component_class()
    if file_cls is None:
        return False

    name = _output_filename(component, image)
    path = _materialize_local_path(component, image)
    link = _component_url(component) or _image_url(image)
    candidates = []
    if path:
        candidates.append((path, ""))
    if link:
        candidates.append((link, ""))
        candidates.append(("", link))
    if not candidates:
        return False

    for file_value, url_value in candidates:
        file_component = _build_file_component(file_cls, name, file_value, url_value)
        if file_component is None:
            continue
        try:
            await event.send(event.chain_result([file_component]))
            logger.info("图片已改以文件形式发送：%s", name)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("以文件形式发送失败：%s", exc)
    return False


async def _send_image_with_fallback(
    event: Any, component: Any, image: Any = None, *, file_fallback: bool = True,
) -> bool:
    """单张图片发送链路：原样 → 压缩 → 文件 → 直链。全失败返回 ``False``。"""
    try:
        await event.send(event.chain_result([component]))
        return True
    except Exception as exc:  # noqa: BLE001
        if _looks_like_upload_limit(exc):
            logger.warning("图片疑似体积过大 / 上传受限，尝试压缩后重试：%s", exc)
        else:
            logger.warning("单条图片发送失败，尝试其它方式：%s", exc)

    # 降级环节一：压缩体积后再按原样试一次（Pillow 不可用时自动跳过）
    compressed = _compressed_component(component, image)
    if compressed is not None:
        compressed_component, compressed_image = compressed
        try:
            await event.send(event.chain_result([compressed_component]))
            logger.info("图片压缩后发送成功")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("压缩后发送仍然失败：%s", exc)
            component, image = compressed_component, compressed_image

    # 降级环节二：改以「文件」形式发送（QQ 图片超限时最稳）
    if file_fallback and await _send_as_file(event, component, image):
        return True

    link = _component_url(component) or _image_url(image)
    if link and await _send_plain(event, link):
        return True

    # 全部手段都失败：交由 send_result 的最终文本兜底统一提示，避免重复消息
    return False


async def _send_final_text_fallback(event: Any, caption: Any, images: Any) -> bool:
    """终极兜底：发送「文案 + 图片链接」，保证用户至少能拿到结果。"""
    lines = []
    text = str(caption or "").strip()
    if text:
        lines.append(text)
    image_links = []
    for image in list(images or []):
        link = _image_url(image)
        if link and link not in image_links:
            image_links.append(link)
    lines.extend(image_links)
    if not lines:
        # 既没有文案也没有可用的图片链接，说明本就无内容可发，不打扰用户
        return False
    delivered = await _send_plain(event, "\n".join(lines))
    # 只有文案发送成功不能算图片投递成功；否则主流程会错误记录为会话成功。
    return bool(delivered and (not images or image_links))


# --------------------------------------------------------------------------- #
# 公开接口：发送
# --------------------------------------------------------------------------- #
def image_to_component(img: "GeneratedImage | ImageInput") -> Any:
    """把 ``GeneratedImage`` / ``ImageInput`` 转成 ``Comp.Image``，无法转换返回 ``None``。"""
    image_cls = _comp_class("Image")
    if image_cls is None:
        return None
    data = getattr(img, "data", None)
    url = getattr(img, "url", None)
    if isinstance(data, (bytes, bytearray)) and len(data) > 0:
        payload = bytes(data)
        from_bytes = getattr(image_cls, "fromBytes", None)
        if callable(from_bytes):
            try:
                return from_bytes(payload)
            except Exception as exc:  # noqa: BLE001
                logger.warning("构造图片组件失败（fromBytes）：%s", exc)
        b64 = base64.b64encode(payload).decode("ascii")
        from_base64 = getattr(image_cls, "fromBase64", None)
        if callable(from_base64):
            try:
                return from_base64(b64)
            except Exception as exc:  # noqa: BLE001
                logger.warning("构造图片组件失败（fromBase64）：%s", exc)
        return None
    if isinstance(url, str) and url.strip():
        from_url = getattr(image_cls, "fromURL", None)
        if callable(from_url):
            try:
                return from_url(url.strip())
            except Exception as exc:  # noqa: BLE001
                logger.warning("构造图片组件失败（fromURL）：%s", exc)
    return None


async def send_result(
    event: Any, result: "GenerateResult", caption: str = "", *, file_fallback: bool = True,
) -> bool:
    """把生成结果（图片 + 文案）发送到当前会话。

    发送链依次降级：整链 → 逐条 → 文件 → 直链 → 纯文本；
    任何异常都只记录日志，绝不向上抛出。
    """
    if event is None:
        return False

    text = str(caption or "").strip()
    plain_cls = _comp_class("Plain")
    text_component = None
    if text and plain_cls is not None:
        try:
            text_component = plain_cls(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("构造文本组件失败：%s", exc)

    chain: list = []
    image_refs: dict = {}
    if text_component is not None:
        chain.append(text_component)

    images = list(getattr(result, "images", None) or [])
    for image in images:
        try:
            component = image_to_component(image)
        except Exception as exc:  # noqa: BLE001
            logger.warning("构造图片组件失败：%s", exc)
            component = None
        if component is None:
            continue
        chain.append(component)
        image_refs[id(component)] = image

    if not chain:
        # 连消息组件都构造不出来（组件库异常等），至少尝试发送文案与图片链接
        return await _send_final_text_fallback(event, caption, images)

    image_count = len(chain) - (1 if text_component is not None else 0)
    if image_count > 1 and _is_qq_official(event):
        # QQ 官方机器人一条消息只能携带一张媒体，多图直接逐条发送，
        # 避免单张超限导致整条消息链一起失败。
        logger.info("QQ 官方机器人多图发送：改为逐条发送 %s 张", image_count)
    else:
        message_chain = None
        try:
            message_chain = event.chain_result(chain)
        except Exception as exc:  # noqa: BLE001
            logger.error("构造消息链失败：%s", exc)

        if message_chain is not None:
            try:
                await event.send(message_chain)
                return image_count == len(images)
            except Exception as exc:  # noqa: BLE001
                logger.error("整链发送失败，尝试逐条发送：%s", exc)

    sent, sent_images = await _send_chain_one_by_one(
        event, chain, image_refs, file_fallback=file_fallback
    )
    if sent <= 0:
        # 终极兜底：全链路都失败时，至少把文案与图片链接发出去
        return await _send_final_text_fallback(event, caption, images)
    return sent_images == len(images)


async def _send_chain_one_by_one(
    event: Any, chain: list, image_refs: Any = None, *, file_fallback: bool = True,
) -> tuple[int, int]:
    """降级发送：逐条发送组件，并对图片复用「原样 → 文件 → 直链 → 文本」链路。

    返回成功发送的组件数量（可能为 0）；任何异常都不向上抛。
    """
    refs = image_refs if isinstance(image_refs, dict) else {}
    sent = 0
    sent_images = 0
    for component in chain:
        try:
            if _is_component(component, "Plain"):
                if await _send_plain(event, getattr(component, "text", "")):
                    sent += 1
                continue
            image = refs.get(id(component))
            if await _send_image_with_fallback(event, component, image, file_fallback=file_fallback):
                sent += 1
                sent_images += 1
        except Exception as exc:  # noqa: BLE001 - 逐条发送也失败，记录后继续
            logger.error("发送消息组件失败：%s", exc)
    return sent, sent_images


__all__ = [
    "AVATAR_FETCH_TIMEOUT",
    "DEFAULT_FETCH_TIMEOUT",
    "MAX_IMAGE_BYTES",
    "QQ_AVATAR_URL_TEMPLATE",
    "ReplyImageList",
    "collect_attachment_images",
    "collect_input_images",
    "collect_mention_images",
    "collect_url_images",
    "extract_image_urls",
    "extract_reply_images",
    "extract_reply_images_async",
    "fetch_image",
    "image_to_component",
    "looks_like_image_url",
    "send_result",
]
