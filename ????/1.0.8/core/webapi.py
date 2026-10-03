"""插件 Web API（设置界面后端）。

路由前缀 = 插件包名 ``/astrbot_plugin_gpt_image``，规格见 ``_docs/CONTRACT_V2.md`` 第 5 节。

设计要点：
- 注册阶段只捕获 ``plugin``，每个请求内重新读取 ``plugin.cfg``（插件重载会替换该对象）。
- 保存配置走「校验 -> 应用 -> cfg.save()」，非法输入返回 400 的 ``error_response``。
- 所有 handler 都有异常兜底，绝不向 Dashboard 抛 500。
- v1.0.1 起：中转站地址与密钥只在「供应商」里维护一份，协议 / 模型按协议维度保存。
"""

from __future__ import annotations

import asyncio
import copy
import functools
import hashlib
import os
import re
import time
from urllib.request import Request as UrlRequest, urlopen
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from astrbot.api.web import error_response, json_response, request

from .config import PluginConfig
from .models import (
    PROTOCOL_KEYS,
    PROTOCOL_LABELS,
    PROTOCOL_SHORT_LABELS,
    Channel,
    default_model_for,
    looks_like_size,
    normalize_protocol,
    normalize_size,
)

PLUGIN_NAME = "astrbot_plugin_gpt_image"
DEFAULT_VERSION = "1.0.8"
UPDATE_BASE_URL = "http://docs.317ak.com/AstrBot/gpt-image-2.5huihua"
UPDATE_CHANGELOG_URL = UPDATE_BASE_URL + "/CHANGELOG.txt"

#: 群聊模式取值
GROUP_MODES: Tuple[str, ...] = ("all", "whitelist", "blacklist")
#: 通过 cfg.set_commands() 写入的指令类列表
COMMAND_KEYS: Tuple[str, ...] = (
    "draw_commands",
    "edit_commands",
    "menu_commands",
    "master_commands",
)
#: 普通字符串列表（用户 ID / 平台 / 群号等）
PLAIN_LIST_KEYS: Tuple[str, ...] = (
    "masters",
    "enabled_platforms",
    "enabled_bot_ids",
    "group_list",
    "user_blacklist",
    "flavor_lines",
)
#: 文案字段（允许空字符串，读取时会回落到默认文案）
TEXT_KEYS: Tuple[str, ...] = (
    "start_prompt", "done_prompt", "menu_text",
    "img2prompt_model", "translate_model", "translate_supplier",
    "translate_prompt_text",
)
#: 整数范围字段：键 -> (最小值, 最大值)
INT_RANGE_KEYS: Dict[str, Tuple[int, int]] = {
    "max_input_images": (1, 20),
    "max_concurrent": (1, 16),
    "cooldown": (0, 3600),
    "retry_times": (0, 3),
    "batch_max": (1, 10),
}
#: 布尔字段
BOOL_KEYS: Tuple[str, ...] = (
    "private_enabled",
    "masters_use_astrbot_admin",
    "reply_reference_image",
    "size_auto_detect", "strict_trigger", "group_require_enable",
    "gacha_enabled", "rank_enabled", "flavor_enabled",
    "translate_enabled", "img2prompt_enabled",
    "transparent_background", "qq_file_fallback", "auto_reload",
)
QUALITY_VALUES = frozenset({"auto", "low", "medium", "high", "xhigh", "max"})
#: 各协议的官方默认地址（前端占位提示用）
DEFAULT_BASE_URLS: Dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com",
    "grok": "https://api.x.ai/v1",
    # 需求 5 新增协议：Flux / 即梦 / 通义走中转站，给一个可辨识的占位地址；
    # SD WebUI / ComfyUI 是自部署服务，没有通用默认值，留空由用户填写。
    "flux": "https://api.example.com/v1",
    "jimeng": "https://api.example.com/v1",
    "tongyi": "https://dashscope.aliyuncs.com/api/v1",
    "sdwebui": "http://127.0.0.1:7860",
    "comfyui": "http://127.0.0.1:8188",
}
#: 单个协议的模型缓存条数上限
MODEL_CACHE_LIMIT = 200


class _ValidationError(ValueError):
    """请求参数校验失败（对外返回 400 的 error_response）。"""


# --------------------------------------------------------------- 类型转换工具
def _text(value: Any) -> str:
    """安全转字符串并裁剪空白。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return ""
    try:
        return str(value).strip()
    except Exception:  # noqa: BLE001
        return ""


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise _ValidationError("%s 必须是字符串" % field)
    text = value.strip()
    if not text:
        raise _ValidationError("%s 不能为空" % field)
    return text


def _int_value(value: Any) -> Optional[int]:
    """宽松转整数：bool 不算整数，接受整数浮点与纯数字字符串。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if float(value).is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[+-]?\d+", text or ""):
            return int(text)
    return None


def _float_value(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _bool_value(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise _ValidationError("%s 必须是布尔值" % field)
    return value


def _str_list(value: Any, field: str) -> List[str]:
    """严格列表解析：必须是 list，元素转字符串后去空、去重。"""
    if not isinstance(value, list):
        raise _ValidationError("%s 必须是数组" % field)
    result: List[str] = []
    for item in value:
        if item is None or isinstance(item, bool):
            continue
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _text_list(value: Any, field: str) -> List[str]:
    if not isinstance(value, list):
        raise _ValidationError("%s 必须是字符串数组" % field)
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise _ValidationError("%s[%d] 必须是字符串" % (field, index))
    return _str_list(value, field)


def _dedupe(items: Sequence[Any]) -> List[str]:
    """去重并去掉空值，保持原顺序。"""
    result: List[str] = []
    for item in items:
        text = _text(item)
        if text and text not in result:
            result.append(text)
    return result


def _model_names(value: Any, depth: int = 0) -> List[str]:
    """归一化协议层或兼容供应商返回的模型目录。

    协议实现通常已经返回字符串列表，但部分中转站会直接返回
    ``data/models/items/results`` 嵌套对象，或使用 ``model_id`` / ``name``
    字段。Web API 在边界统一处理，避免前端因供应商格式差异拿不到模型。
    """
    if depth > 6:
        return []
    if isinstance(value, str):
        return _dedupe([value])
    if isinstance(value, (list, tuple, set)):
        result: List[str] = []
        for item in value:
            result.extend(_model_names(item, depth + 1))
        return _dedupe(result)
    if not isinstance(value, dict):
        return []

    result: List[str] = []
    for key in (
        "id", "model_id", "modelId", "slug", "name", "model", "model_name",
        "modelName", "deployment", "deployment_name", "engine",
    ):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            result.append(candidate)
            break
    for key in (
        "model", "data", "models", "items", "results", "list", "choices",
        "response", "result", "output", "content", "model_list", "modelList",
        "available_models", "availableModels",
    ):
        nested = value.get(key)
        if nested is not None and not (key == "model" and isinstance(nested, str)):
            if key in {"models", "model_list", "modelList", "available_models", "availableModels"} and isinstance(nested, dict):
                for model_key, model_value in nested.items():
                    if isinstance(model_value, (dict, list, tuple)):
                        result.extend(_model_names(model_value, depth + 1))
                        if isinstance(model_key, str) and model_key.strip():
                            result.append(model_key)
            else:
                result.extend(_model_names(nested, depth + 1))
    if not result and value:
        for model_key, model_value in value.items():
            if not isinstance(model_key, str) or not model_key.strip():
                continue
            if isinstance(model_value, (dict, list, tuple)):
                nested_names = _model_names(model_value, depth + 1)
                if nested_names or model_key not in {
                    "object", "type", "status", "success", "message", "error",
                    "pagination", "meta", "metadata", "usage",
                }:
                    result.append(model_key)
                    result.extend(nested_names)
    return _dedupe(result)


def _public_value(value: Any, key: str = "", depth: int = 0) -> Any:
    """递归脱敏供应商附加配置，保留 UI 所需的完整非敏感数据。"""
    if re.search(r"(?i)(key|secret|token|password|credential|authorization|cookie)", key):
        return None
    if depth > 4:
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for item_key, item_value in value.items():
            safe = _public_value(item_value, str(item_key), depth + 1)
            if safe is not None:
                result[str(item_key)] = safe
        return result
    if isinstance(value, (list, tuple)):
        return [
            safe
            for item in value[:64]
            if (safe := _public_value(item, key, depth + 1)) is not None
        ]
    return _text(value)


def _sizes_list(value: Any, field: str) -> List[str]:
    """尺寸列表：必须是 list，逐项规范化、去重，结果不允许为空。"""
    if not isinstance(value, list):
        raise _ValidationError("%s 必须是数组" % field)
    result: List[str] = []
    for item in value:
        if item is None or isinstance(item, bool):
            continue
        text = str(item).strip()
        if not text:
            continue
        normalized = normalize_size(text, "")
        if not normalized or not looks_like_size(normalized):
            raise _ValidationError("%s 含无效尺寸：%s" % (field, text))
        if normalized not in result:
            result.append(normalized)
    if not result:
        raise _ValidationError("%s 不能为空" % field)
    return result


def _mask_key(api_key: Any) -> str:
    """把密钥打码（规则与 Channel.masked_key / config._mask_api_key 完全一致）。"""
    text = _text(api_key)
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return "%s****%s" % (text[:4], text[-4:])


def _safe_url(value: Any) -> str:
    """返回可公开展示的 URL，隐藏 query 中常见的密钥参数和 URL 凭据。"""
    raw = _text(value)
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        if not parts.scheme or not parts.netloc:
            return re.sub(
                r"(?i)([?&](?:api[_-]?key|key|token|secret|password)=)[^&#]*",
                r"\1***",
                raw,
            )
        try:
            hostname = parts.hostname or ""
            if ":" in hostname and not hostname.startswith("["):
                hostname = "[%s]" % hostname
            netloc = hostname
            if parts.port:
                netloc += ":%d" % parts.port
        except ValueError:
            netloc = re.sub(r"^[^@]+@", "", parts.netloc)
        query: List[Tuple[str, str]] = []
        for key, item in parse_qsl(parts.query, keep_blank_values=True):
            if re.search(r"(?i)(key|token|secret|password|credential|auth)", key):
                item = "***"
            query.append((key, item))
        return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), ""))
    except Exception:  # noqa: BLE001 - 展示信息脱敏失败时仍不暴露原始凭据
        return re.sub(
            r"(?i)([?&](?:api[_-]?key|key|token|secret|password)=)[^&#]*",
            r"\1***",
            raw,
        )


def _safe_error(exc: Any) -> Dict[str, Any]:
    """把协议异常转换成前端可读且不含密钥的诊断错误。"""
    status = getattr(exc, "status", None)
    if not isinstance(status, int):
        status = None
    message = _text(exc)
    detail = _text(getattr(exc, "detail", ""))
    combined = re.sub(
        r"(?i)((?:bearer|api[_-]?key|key|token|secret|password)\s*[:=]\s*)[^\s,;]+",
        r"\1***",
        message,
    )
    safe_detail = re.sub(
        r"(?i)((?:bearer|api[_-]?key|key|token|secret|password)\s*[:=]\s*)[^\s,;]+",
        r"\1***",
        detail,
    )
    return {"message": combined or type(exc).__name__, "detail": safe_detail, "http_status": status}


# ------------------------------------------------------------------ 配置读取
def _prefer(cfg: Any, methods: Sequence[str]) -> Optional[Any]:
    """返回第一个存在且可调用的方法（用于新旧接口兼容）。"""
    for name in methods:
        func = getattr(cfg, name, None)
        if callable(func):
            return func
    return None


def _call_text(cfg: Any, method: str, *args: Any) -> str:
    func = getattr(cfg, method, None)
    if not callable(func):
        return ""
    try:
        return _text(func(*args))
    except Exception:  # noqa: BLE001
        return ""


def _call_list(cfg: Any, method: str, *args: Any) -> List[str]:
    func = getattr(cfg, method, None)
    if not callable(func):
        return []
    try:
        value = func(*args)
    except Exception:  # noqa: BLE001
        return []
    if isinstance(value, (list, tuple)):
        return [_text(item) for item in value if _text(item)]
    return []


def _call_number(cfg: Any, method: str, default: float = 0) -> float:
    """调用返回数值的配置方法，保证输出是数字（JSON 里不能是字符串）。"""
    func = getattr(cfg, method, None)
    if not callable(func):
        return default
    try:
        value = func()
    except Exception:  # noqa: BLE001
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _raw_supplier_entries(cfg: Any) -> List[Dict[str, Any]]:
    """读取原始供应商条目，保留兼容字段供 WebUI 展示。

    ``PluginConfig.suppliers()`` 故意只暴露运行时所需的三项连接信息，
    但 AstrBot 的旧版 ``providers``、嵌套模板和第三方扩展字段仍应在
    设置页可见。因此这里只读原始配置，并按新键优先、旧键回退去重。
    """
    raw = None
    for method in ("raw_dict", "raw"):
        func = getattr(cfg, method, None)
        if callable(func):
            try:
                candidate = func()
            except Exception:  # noqa: BLE001
                candidate = None
            if isinstance(candidate, dict):
                raw = candidate
                break
    if not isinstance(raw, dict):
        return []

    sources: List[Any] = [raw.get("suppliers")]
    nested = raw.get("nested") if isinstance(raw.get("nested"), dict) else {}
    nested_suppliers = nested.get("suppliers") if isinstance(nested, dict) else {}
    if isinstance(nested_suppliers, dict):
        sources.append(nested_suppliers.get("suppliers"))
    sources.extend((raw.get("providers"), raw.get("channels")))
    if isinstance(nested_suppliers, dict):
        sources.extend((nested_suppliers.get("providers"), nested_suppliers.get("channels")))

    result: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def append_entry(entry: Any, fallback_name: str = "") -> None:
        if not isinstance(entry, dict):
            return
        value = dict(entry)
        for wrapper in ("config", "data", "settings", "value"):
            nested_value = value.get(wrapper)
            if isinstance(nested_value, dict):
                merged = dict(nested_value)
                merged.update({key: item for key, item in value.items() if key != wrapper})
                value = merged
        name = _text(value.get("name") or value.get("title") or value.get("id") or fallback_name)
        if not name:
            return
        key = name.casefold()
        if key in seen:
            return
        seen.add(key)
        value["name"] = name
        result.append(value)

    for source in sources:
        if isinstance(source, list):
            for entry in source:
                append_entry(entry)
        elif isinstance(source, dict):
            for name, entry in source.items():
                append_entry(entry, _text(name))
    return result


def _raw_supplier_by_name(cfg: Any) -> Dict[str, Dict[str, Any]]:
    return {item["name"].casefold(): item for item in _raw_supplier_entries(cfg)}


def _existing_suppliers(cfg: Any) -> List[Channel]:
    """读取全部供应商（兼容旧版 channels() 接口）。"""
    result: List[Channel] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        channel = value if isinstance(value, Channel) else Channel.from_config_entry(value)
        if channel is None or not channel.name:
            return
        key = channel.name.casefold()
        if key in seen:
            return
        seen.add(key)
        result.append(channel)

    for name in ("suppliers", "channels"):
        func = getattr(cfg, name, None)
        if not callable(func):
            continue
        try:
            items = list(func() or [])
        except Exception:  # noqa: BLE001
            continue
        for item in items:
            add(item)

    if result:
        return result

    raw = None
    for name in ("raw_dict", "raw"):
        func = getattr(cfg, name, None)
        if callable(func):
            try:
                raw = func()
            except Exception:  # noqa: BLE001
                raw = None
            if isinstance(raw, dict):
                break
    if not isinstance(raw, dict):
        return result

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(value, list):
            for item in value:
                add(item)
            return
        if not isinstance(value, dict):
            return
        if any(key in value for key in ("name", "base_url", "api_key", "token", "secret")):
            add(value)
        for key in ("suppliers", "providers", "channels"):
            if key in value:
                nested_value = value.get(key)
                if isinstance(nested_value, dict):
                    # 一些 AstrBot/中转站配置把供应商写成 {名称: 配置} 映射。
                    for item_name, item in nested_value.items():
                        if isinstance(item, dict):
                            candidate = dict(item)
                            candidate.setdefault("name", item_name)
                            add(candidate)
                walk(nested_value, depth + 1)
        for key in ("nested", "config", "data", "settings", "value"):
            nested = value.get(key)
            if isinstance(nested, (dict, list)):
                walk(nested, depth + 1)

    walk(raw)
    return result


def _supplier_names(cfg: Any) -> List[str]:
    for name in ("supplier_names", "channel_names"):
        values = _call_list(cfg, name)
        if values:
            return values
    return [item.name for item in _existing_suppliers(cfg)]


def _get_supplier(cfg: Any, name: str) -> Optional[Channel]:
    target = _text(name)
    if not target:
        return None
    func = _prefer(cfg, ("get_supplier", "get_channel"))
    if func is not None:
        try:
            found = func(target)
        except Exception:  # noqa: BLE001
            found = None
        if isinstance(found, Channel):
            return found
    lowered = target.lower()
    for item in _existing_suppliers(cfg):
        if item.name == target or item.name.lower() == lowered:
            return item
    names = _supplier_names(cfg)
    if target.isdigit():
        index = int(target) - 1
        if 0 <= index < len(names):
            return _get_supplier(cfg, names[index])
    return None


def _protocol_models_public(cfg: Any) -> Dict[str, Dict[str, str]]:
    func = getattr(cfg, "protocol_models", None)
    if callable(func):
        try:
            value = func()
        except Exception:  # noqa: BLE001
            value = None
        if isinstance(value, dict) and value:
            result: Dict[str, Dict[str, str]] = {}
            for key in PROTOCOL_KEYS:
                bucket = value.get(key) or {}
                if not isinstance(bucket, dict):
                    bucket = {}
                result[key] = {
                    "model": _text(bucket.get("model")),
                    "edit_model": _text(bucket.get("edit_model")),
                }
            return result
    return {
        key: {
            "model": _call_text(cfg, "model_for", key) or default_model_for(key),
            "edit_model": _call_text(cfg, "edit_model_for", key),
        }
        for key in PROTOCOL_KEYS
    }


def _models_for(cfg: Any, protocol: str, channel: Optional[Channel] = None) -> List[str]:
    """合并配置模型与当前供应商的绘画目录，忽略来源不明的旧协议缓存。"""
    result: List[str] = []
    for method in ("model_for", "edit_model_for"):
        value = _call_text(cfg, method, protocol)
        if value and value not in result:
            result.append(value)
    if channel is None:
        name = _call_text(cfg, "active_supplier_name") or _call_text(cfg, "active_channel_name")
        channel = _protocol_model(None, cfg, name, protocol)
    if channel is not None:
        result.extend(_model_cache(cfg, channel, "image"))
    return _dedupe(result)


def _model_cache_key(cfg: Any, channel: Channel, scope: str) -> Tuple[str, ...]:
    """按供应商、地址、凭据、协议和用途隔离目录；配置变化后自动失效。"""
    return (
        channel.name,
        channel.base_url,
        hashlib.sha256(_text(channel.api_key).encode("utf-8")).hexdigest(),
        channel.protocol,
        scope,
        _call_text(cfg, "proxy") or _text((channel.extra or {}).get("proxy")),
    )


def _model_cache(cfg: Any, channel: Channel, scope: str) -> List[str]:
    """读取当前配置实例的独立目录缓存，不访问持久化的绘画缓存。"""
    cache = getattr(cfg, "_webapi_model_catalogs", None)
    if not isinstance(cache, dict):
        return []
    value = cache.get(_model_cache_key(cfg, channel, scope))
    if not isinstance(value, (list, tuple)):
        return []
    return list(value)


def _set_model_cache(
    cfg: Any, channel: Channel, scope: str, models: Sequence[str]
) -> None:
    """缓存目录至内存；完整目录不截断，最多保留 64 组查询结果。"""
    cache = getattr(cfg, "_webapi_model_catalogs", None)
    if not isinstance(cache, dict):
        cache = {}
        cfg._webapi_model_catalogs = cache
    key = _model_cache_key(cfg, channel, scope)
    cache.pop(key, None)
    values = _dedupe(models)
    cache[key] = values[:MODEL_CACHE_LIMIT] if scope == "image" else values
    while len(cache) > 64:
        cache.pop(next(iter(cache)))

def _protocol_model(plugin: Any, cfg: Any, name: str, protocol: str, model: str = "") -> Optional[Channel]:
    """按「供应商 + 协议 + 模型」组装一个可直接请求的通道。"""
    builder = getattr(cfg, "build_channel", None)
    if callable(builder):
        try:
            channel = builder(supplier=name, protocol=protocol, model=model)
        except Exception:  # noqa: BLE001
            channel = None
        if isinstance(channel, Channel):
            return channel

    base = _get_supplier(cfg, name)
    if base is None:
        return None
    return Channel(
        protocol=protocol or base.protocol,
        name=base.name,
        base_url=_text(base.base_url),
        api_key=_text(base.api_key),
        model=_text(model) or _call_text(cfg, "model_for", protocol) or default_model_for(protocol),
        edit_model=_call_text(cfg, "edit_model_for", protocol),
        extra=dict(getattr(base, "extra", None) or {}),
    )


# -------------------------------------------------------------- 配置写入计划
def _truthy(value: Any) -> bool:
    """宽松布尔判断：接受 True / 1 / "1" / "true" / "yes" / "on"。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def _same_site(left: Any, right: Any) -> bool:
    """判断两个 Base URL 是否指向同一站点（用于识别「只改名」的供应商条目）。

    ``left`` 缺省（请求体没带 base_url）时返回 True：此时无法判断站点是否变化，
    按「只改名」处理，避免把已保存的密钥误清空。
    """
    first = _text(left).rstrip("/").lower()
    if not first:
        return True
    return first == _text(right).rstrip("/").lower()


def _unique_old_by_site(
    old_entries: Sequence[Channel], submitted_names: Sequence[str], base_url: Any
) -> Optional[Channel]:
    """按 Base URL 找「本次未提交」的旧条目，且只在唯一命中时返回，否则返回 None。

    用于「删掉一个供应商 + 把另一个改名」时仍能找回原条目的密钥（名称和位置都对
    不上，但站点一致，密钥本就属于该站点，不会串到别的域名）。
    """
    target = _text(base_url).rstrip("/").lower()
    if not target:
        return None
    matches = [
        item
        for item in old_entries
        if item.name not in submitted_names
        and _text(item.base_url).rstrip("/").lower() == target
    ]
    return matches[0] if len(matches) == 1 else None


def _parse_suppliers(cfg: Any, raw: Any) -> List[Channel]:
    """解析 suppliers 数组；空 api_key 会沿用旧密钥（含「改名」场景）。

    前端密钥框恒为空并承诺「留空表示不修改」，因此改名后的供应商在旧配置里
    按名称查不到，需要按「同位置」借用旧条目，否则密钥会被写成空串。
    """
    if not isinstance(raw, list):
        raise _ValidationError("suppliers 必须是数组")
    if not raw:
        raise _ValidationError("suppliers 不能为空，至少保留一个供应商")

    # 旧条目保持配置文件里的原始顺序（_existing_suppliers 直接遍历 cfg.suppliers()，
    # 顺序稳定），下面的「同位置兜底」依赖该顺序。
    old_entries: List[Channel] = _existing_suppliers(cfg)
    existing: Dict[str, Channel] = {channel.name: channel for channel in old_entries}

    # 本次提交里出现过的全部名称，用于判断某个旧条目是被「改名」还是被「删除」
    submitted_names: List[str] = []
    for item in raw:
        if isinstance(item, dict):
            item_name = _text(item.get("name"))
            if item_name:
                submitted_names.append(item_name)

    suppliers: List[Channel] = []
    seen: List[str] = []
    for index, entry in enumerate(raw):
        label = "suppliers[%d]" % index
        if not isinstance(entry, dict):
            raise _ValidationError("%s 必须是对象" % label)

        name = _require_text(entry.get("name"), label + ".name")
        if name in seen:
            raise _ValidationError("供应商名称重复：%s" % name)

        # 旧条目查找分三层：① 按名称精确匹配；② 按同位置借用（改名场景）；
        # ③ 按 Base URL 唯一匹配（删一个 + 改名导致位置错位时兜底）。
        old = existing.get(name)
        if old is None and index < len(old_entries):
            # 改名场景：改名后的名称在旧配置里必然查不到，而前端密钥框恒为空并
            # 承诺「留空表示不修改」，只能按同位置借用旧条目，避免把已保存的密钥
            # 清空。借用需要同时满足两个前提，避免把别人的密钥装到新条目上：
            #   1) 该位置的旧条目没有出现在本次提交里（不是「移动」而是「改名」）；
            #   2) 新旧 Base URL 指向同一站点（改地址时新旧密钥本就不通用）。
            candidate = old_entries[index]
            if candidate.name not in submitted_names and _same_site(
                entry.get("base_url"), candidate.base_url
            ):
                old = candidate
        if old is None:
            old = _unique_old_by_site(
                old_entries, submitted_names, entry.get("base_url")
            )

        base_url_raw = entry.get("base_url")
        if base_url_raw is None:
            base_url = _text(old.base_url) if old is not None else ""
        elif not isinstance(base_url_raw, str):
            raise _ValidationError("%s.base_url 必须是字符串" % label)
        else:
            base_url = base_url_raw.strip()

        api_key_raw = entry.get("api_key")
        if api_key_raw is not None and not isinstance(api_key_raw, str):
            raise _ValidationError("%s.api_key 必须是字符串" % label)
        api_key = (api_key_raw or "").strip()
        if _truthy(entry.get("api_key_clear")):
            # 显式清空：前端密钥框恒为空，仅靠留空无法区分「不修改」与「要清空」，
            # 需要该标志位。
            api_key = ""
        elif not api_key and old is not None:
            api_key = _text(old.api_key)

        suppliers.append(
            Channel(
                protocol="openai",
                name=name,
                base_url=base_url,
                api_key=api_key,
            )
        )
        seen.append(name)
    return suppliers


def _request_suppliers(payload: Dict[str, Any]) -> Tuple[bool, Any]:
    """从新旧及嵌套请求体中提取供应商数组。"""
    def find(value: Any, depth: int = 0) -> Tuple[bool, Any]:
        if depth > 5 or not isinstance(value, dict):
            return False, None
        for key in ("suppliers", "providers", "channels"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                return True, candidate
            if isinstance(candidate, dict):
                found, nested_value = find(candidate, depth + 1)
                if found:
                    return True, nested_value
        for key in ("nested", "config", "data", "settings", "value"):
            found, nested_value = find(value.get(key), depth + 1)
            if found:
                return True, nested_value
        return False, None

    return find(payload)


def _parse_protocol_models(cfg: Any, raw: Any) -> Dict[str, Dict[str, str]]:
    """解析 protocol_models；缺省协议沿用旧值。"""
    if not isinstance(raw, dict):
        raise _ValidationError("protocol_models 必须是对象")

    current = _protocol_models_public(cfg)
    result: Dict[str, Dict[str, str]] = {}
    for key in PROTOCOL_KEYS:
        base = current.get(key) or {"model": "", "edit_model": ""}
        model = _text(base.get("model"))
        edit_model = _text(base.get("edit_model"))
        bucket = raw.get(key)
        if bucket is not None:
            if not isinstance(bucket, dict):
                raise _ValidationError("protocol_models.%s 必须是对象" % key)
            if "model" in bucket:
                value = bucket.get("model")
                if value is not None and not isinstance(value, str):
                    raise _ValidationError("protocol_models.%s.model 必须是字符串" % key)
                model = (value or "").strip()
            if "edit_model" in bucket:
                value = bucket.get("edit_model")
                if value is not None and not isinstance(value, str):
                    raise _ValidationError("protocol_models.%s.edit_model 必须是字符串" % key)
                edit_model = (value or "").strip()
        result[key] = {"model": model, "edit_model": edit_model}
    return result


def _structured_list(value: Any, key: str, limit: int) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        raise _ValidationError("%s 必须是数组" % key)
    if len(value) > limit:
        raise _ValidationError("%s 最多 %d 项" % (key, limit))
    result: List[Dict[str, Any]] = []
    names: set = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise _ValidationError("%s[%d] 必须是对象" % (key, index))
        name = item.get("name")
        if not isinstance(name, str):
            raise _ValidationError("%s[%d].name 必须是字符串" % (key, index))
        name = name.strip()
        if not name:
            raise _ValidationError("%s[%d].name 不能为空" % (key, index))
        if name in names:
            raise _ValidationError("%s 存在重复名称：%s" % (key, name))
        names.add(name)
        result.append(dict(item, name=name))
    return result


def _parse_new_structured(payload: Dict[str, Any], plan: Dict[str, Any]) -> None:
    if "menu_sections" in payload:
        entries = _structured_list(payload["menu_sections"], "menu_sections", 20)
        for index, entry in enumerate(entries):
            items = entry.get("items", [])
            if not isinstance(items, list) or len(items) > 40:
                raise _ValidationError("menu_sections[%d].items 必须是最多 40 项的数组" % index)
            entry["items"] = _text_list(items, "menu_sections[%d].items" % index)
        plan["menu_sections"] = entries
    for key in ("prompts", "gacha_styles"):
        if key in payload:
            entries = _structured_list(payload[key], key, 200)
            for index, entry in enumerate(entries):
                label = "%s[%d]" % (key, index)
                entry["text"] = _require_text(entry.get("text"), label + ".text")
                if key == "prompts":
                    entry["tags"] = _text_list(entry.get("tags", []), label + ".tags")
                else:
                    rarity = entry.get("rarity", "common")
                    if not isinstance(rarity, str) or rarity not in ("common", "rare", "epic"):
                        raise _ValidationError("%s.rarity 必须是 common / rare / epic" % label)
                    entry["rarity"] = rarity
            plan[key] = entries
    if "bot_overrides" in payload:
        value = payload["bot_overrides"]
        if not isinstance(value, dict) or len(value) > 50:
            raise _ValidationError("bot_overrides 必须是最多 50 个实例的对象")
        overrides: Dict[str, Dict[str, str]] = {}
        seen_ids: set = set()
        for platform_id, raw in value.items():
            if not isinstance(platform_id, str):
                raise _ValidationError("bot_overrides 的实例 ID 必须是字符串")
            pid = platform_id.strip()
            if not pid or not isinstance(raw, dict):
                raise _ValidationError("bot_overrides 的实例 ID 和配置格式不正确")
            if pid in seen_ids:
                raise _ValidationError("bot_overrides 存在重复实例 ID：%s" % pid)
            seen_ids.add(pid)
            if set(raw) - {"supplier", "protocol", "model", "size"}:
                raise _ValidationError("bot_overrides.%s 含未知配置字段" % pid)
            item: Dict[str, str] = {}
            for field in ("supplier", "protocol", "model", "size"):
                if field in raw:
                    field_value = raw.get(field)
                    if not isinstance(field_value, str):
                        raise _ValidationError("bot_overrides.%s 必须是字符串" % field)
                    item[field] = field_value.strip()
            if "protocol" in item and item["protocol"]:
                protocol = normalize_protocol(item["protocol"])
                if not protocol:
                    raise _ValidationError("bot_overrides.protocol 不是有效协议")
                item["protocol"] = protocol
            if "size" in item and item["size"]:
                size = normalize_size(item["size"], "")
                if not size or not looks_like_size(size):
                    raise _ValidationError("bot_overrides.size 不是有效尺寸")
                item["size"] = size
            item = {field: text for field, text in item.items() if text}
            if item:
                overrides[pid] = item
        plan["bot_overrides"] = overrides


def _build_plan(cfg: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    """校验请求体并生成「待应用」的值；不修改 cfg。"""
    plan: Dict[str, Any] = {}

    has_suppliers, suppliers_raw = _request_suppliers(payload)
    if has_suppliers:
        plan["suppliers"] = _parse_suppliers(cfg, suppliers_raw)

    if "active_supplier" in payload or "active_provider" in payload:
        name = _text(payload.get("active_supplier")) or _text(payload.get("active_provider"))
        if not name:
            raise _ValidationError("active_supplier 不能为空")
        if "suppliers" in plan:
            names = [item.name for item in plan["suppliers"]]
        else:
            names = _supplier_names(cfg)
        if name not in names:
            raise _ValidationError("供应商不存在：%s" % name)
        plan["active_supplier"] = name

    if "active_protocol" in payload:
        protocol = normalize_protocol(_text(payload.get("active_protocol")))
        if not protocol:
            raise _ValidationError("active_protocol 必须是 openai / gemini / grok 之一")
        plan["active_protocol"] = protocol

    if "protocol_models" in payload:
        plan["protocol_models"] = _parse_protocol_models(cfg, payload.get("protocol_models"))

    if "trigger_mode" in payload:
        mode = _require_text(payload.get("trigger_mode"), "trigger_mode").lower()
        if mode not in ("at", "command"):
            raise _ValidationError("trigger_mode 必须是 at / command 之一")
        plan["trigger_mode"] = mode

    for key, (low, high) in INT_RANGE_KEYS.items():
        if key in payload:
            value = _int_value(payload.get(key))
            if value is None or value < low or value > high:
                raise _ValidationError("%s 必须是 %d~%d 的整数" % (key, low, high))
            plan[key] = value

    if "proxy" in payload:
        value = payload.get("proxy")
        if value is None:
            plan["proxy"] = ""
        elif not isinstance(value, str):
            raise _ValidationError("proxy 必须是字符串")
        else:
            plan["proxy"] = value.strip()

    if "image_sizes" in payload:
        plan["image_sizes"] = _sizes_list(payload.get("image_sizes"), "image_sizes")

    if "active_size" in payload:
        size = _require_text(payload.get("active_size"), "active_size")
        normalized = normalize_size(size, "")
        if not normalized or not looks_like_size(normalized):
            raise _ValidationError("尺寸格式不正确：%s" % size)
        plan["active_size"] = normalized

    if "quality" in payload:
        quality = _require_text(payload.get("quality"), "quality").lower()
        if quality not in QUALITY_VALUES:
            raise _ValidationError("quality 必须是 auto / low / medium / high / xhigh / max")
        plan["quality"] = quality

    if "timeout" in payload:
        seconds = _float_value(payload.get("timeout"))
        if seconds is None or seconds <= 0:
            raise _ValidationError("timeout 必须是大于 0 的数字")
        plan["timeout"] = seconds

    for key in BOOL_KEYS:
        if key in payload:
            plan[key] = _bool_value(payload.get(key), key)

    if "group_mode" in payload:
        mode = _require_text(payload.get("group_mode"), "group_mode").lower()
        if mode not in GROUP_MODES:
            raise _ValidationError("group_mode 必须是 all / whitelist / blacklist 之一")
        plan["group_mode"] = mode

    for key in COMMAND_KEYS + PLAIN_LIST_KEYS + ("supplier_commands",):
        if key in payload:
            parser = _text_list if key in ("flavor_lines", "user_blacklist") else _str_list
            plan[key] = parser(payload.get(key), key)

    for key in TEXT_KEYS:
        if key in payload:
            value = payload.get(key)
            if not isinstance(value, str):
                raise _ValidationError("%s 必须是字符串" % key)
            plan[key] = value

    _parse_new_structured(payload, plan)

    return plan


def _apply_sizes(cfg: Any, sizes: List[str]) -> None:
    """写回尺寸列表（同时同步 schema 键 image_sizes，方便 Dashboard 配置页展示）。"""
    normalized: List[str] = []
    for size in sizes:
        text = normalize_size(str(size), "")
        if text and text not in normalized:
            normalized.append(text)
    if not normalized:
        return
    cfg.set_raw("sizes", normalized)
    cfg.set_raw("image_sizes", normalized)


def _sync_sizes(cfg: Any) -> None:
    func = getattr(cfg, "image_sizes", None)
    if callable(func):
        try:
            cfg.set_raw("image_sizes", func())
        except Exception:  # noqa: BLE001
            pass

def _apply_plan(cfg: Any, plan: Dict[str, Any]) -> None:
    """把校验通过的计划写入 cfg（不负责 save）。"""
    if "suppliers" in plan:
        suppliers = plan["suppliers"]
        names = [item.name for item in suppliers]
        # 优先整体替换（可正确删除旧条目），旧实现只能逐个 upsert / remove
        replace = getattr(cfg, "replace_suppliers", None)
        if callable(replace):
            try:
                replace(list(suppliers))
            except Exception:  # noqa: BLE001
                pass
        else:
            remover = _prefer(cfg, ("remove_supplier", "remove_channel"))
            upsert = _prefer(cfg, ("upsert_supplier", "upsert_channel"))
            for name in _supplier_names(cfg):
                if name in names or remover is None:
                    continue
                try:
                    remover(name)
                except Exception:  # noqa: BLE001
                    pass
            if upsert is not None:
                for item in suppliers:
                    try:
                        upsert(item)
                    except Exception:  # noqa: BLE001
                        pass

    if "image_sizes" in plan:
        _apply_sizes(cfg, plan["image_sizes"])

    if "active_supplier" in plan:
        setter = _prefer(cfg, ("set_active_supplier", "set_active_channel"))
        if setter is not None:
            try:
                setter(plan["active_supplier"])
            except Exception:  # noqa: BLE001
                pass

    if "active_protocol" in plan:
        setter = getattr(cfg, "set_active_protocol", None)
        if callable(setter):
            try:
                setter(plan["active_protocol"])
            except Exception:  # noqa: BLE001
                pass

    if "protocol_models" in plan:
        for key, bucket in (plan["protocol_models"] or {}).items():
            model = _text(bucket.get("model"))
            edit_model = _text(bucket.get("edit_model"))
            set_model = getattr(cfg, "set_model_for", None)
            set_edit = getattr(cfg, "set_edit_model_for", None)
            if model and callable(set_model):
                try:
                    set_model(key, model)
                except Exception:  # noqa: BLE001
                    pass
            if callable(set_edit):
                try:
                    set_edit(key, edit_model)
                except Exception:  # noqa: BLE001
                    pass

    if "active_size" in plan:
        cfg.set_active_size(plan["active_size"])
    if "timeout" in plan:
        cfg.set_timeout(plan["timeout"])
    if "private_enabled" in plan:
        cfg.set_private_enabled(plan["private_enabled"])
    if "group_mode" in plan:
        cfg.set_group_mode(plan["group_mode"])

    for key in BOOL_KEYS:
        if key in plan and key not in ("private_enabled", "group_require_enable"):
            cfg.set_raw(key, plan[key])
    for key in INT_RANGE_KEYS:
        if key in plan:
            cfg.set_raw(key, plan[key])
    if "proxy" in plan:
        cfg.set_raw("proxy", plan["proxy"])
    if "trigger_mode" in plan:
        cfg.set_raw("trigger_mode", plan["trigger_mode"])
    for key in COMMAND_KEYS:
        if key in plan:
            cfg.set_commands(key, plan[key])
    if "supplier_commands" in plan:
        cfg.set_raw("supplier_commands", plan["supplier_commands"])
    for key in PLAIN_LIST_KEYS:
        if key in plan:
            cfg.set_raw(key, plan[key])
    if "group_require_enable" in plan:
        cfg.set_group_require_enable(plan["group_require_enable"])
    for key in TEXT_KEYS:
        if key in plan:
            if key in ("start_prompt", "done_prompt", "menu_text"):
                cfg.set_prompt(key, plan[key])
            else:
                cfg.set_raw(key, plan[key])
    if "menu_sections" in plan:
        setter = getattr(cfg, "set_menu_sections", None)
        if callable(setter):
            setter(plan["menu_sections"])
        else:
            cfg.set_raw("menu_sections", plan["menu_sections"])
    if "prompts" in plan:
        setter = getattr(cfg, "set_prompts", None)
        if callable(setter):
            setter(plan["prompts"])
        else:
            cfg.set_raw("prompts", plan["prompts"])
    if "gacha_styles" in plan:
        setter = getattr(cfg, "set_gacha_styles", None)
        if callable(setter):
            setter(plan["gacha_styles"])
        else:
            cfg.set_raw("gacha_styles", plan["gacha_styles"])
    if "bot_overrides" in plan:
        cfg.set_raw("bot_overrides", plan["bot_overrides"])
    if "quality" in plan:
        setter = getattr(cfg, "set_quality", None)
        if callable(setter):
            setter(plan["quality"])
        else:
            cfg.set_raw("quality", plan["quality"])


def _snapshot_config(cfg: Any) -> Optional[Tuple[Any, Any]]:
    """复制可变配置内容，供保存失败时恢复内存状态。"""
    getter = getattr(cfg, "raw", None)
    raw = getter() if callable(getter) else None
    if not isinstance(raw, dict):
        return None
    try:
        return raw, copy.deepcopy(dict(raw))
    except Exception:  # noqa: BLE001 - 非标准配置对象无法安全复制时放弃回滚
        return None


def _restore_config(snapshot: Optional[Tuple[Any, Any]], cfg: Any) -> bool:
    """恢复保存前的原始配置并刷新派生缓存。"""
    if not snapshot:
        return False
    raw, original = snapshot
    if not isinstance(raw, dict):
        return False
    try:
        raw.clear()
        raw.update(copy.deepcopy(original))
        reload_config = getattr(cfg, "reload", None)
        if callable(reload_config):
            reload_config()
        return True
    except Exception:  # noqa: BLE001 - 回滚失败只能由调用方返回原始保存错误
        return False


# -------------------------------------------------------------------- 工具集
def _create_protocol(
    channel: Channel, timeout: float, proxy: str = "", include_non_image: bool = False
) -> Any:
    """延迟导入协议层，避免 WebAPI 模块的导入期依赖。"""
    from .protocols import create_protocol

    if include_non_image and channel.protocol not in ("openai", "gemini", "grok", "flux"):
        raise ValueError("该协议不支持文本/视觉模型目录，请手动填写模型")
    return create_protocol(channel, timeout=timeout, proxy=proxy)


def _read_version() -> str:
    """从插件根目录 metadata.yaml 读取版本号，失败时退回常量。"""
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "metadata.yaml")
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        match = re.search(r"^\s*version\s*:\s*[\"']?([^\"'\s#]+)", text, re.MULTILINE)
        if match:
            version = match.group(1).strip()
            if version:
                return version
    except Exception:  # noqa: BLE001 - 版本号读取失败不影响运行
        pass
    return DEFAULT_VERSION


def _protocol_options() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for index, key in enumerate(PROTOCOL_KEYS, start=1):
        items.append(
            {
                "key": key,
                "index": index,
                "label": PROTOCOL_SHORT_LABELS.get(key, key),
                "description": PROTOCOL_LABELS.get(key, key),
                "default_base_url": DEFAULT_BASE_URLS.get(key, ""),
            }
        )
    return items


def _empty_stats() -> Dict[str, Any]:
    """统计模块不可用时的兜底结构（与 core/stats.py 保持一致）。"""
    try:
        from .stats import PluginStats

        return PluginStats().snapshot()
    except Exception:  # noqa: BLE001
        return {
            "totals": {
                "success": 0,
                "failed": 0,
                "images": 0,
                "total": 0,
                "avg_seconds": 0.0,
                "last_error": "",
                "last_time": "",
            },
            "recent": [],
            "started_at": "",
        }


_STATS_STATUS_ORDER: Tuple[str, ...] = (
    "success",
    "failed",
    "partial",
    "cancelled",
    "running",
    "pending",
    "unknown",
)
_STATS_STATUS_ALIASES: Dict[str, str] = {
    "ok": "success",
    "succeeded": "success",
    "done": "success",
    "finished": "success",
    "fail": "failed",
    "failure": "failed",
    "error": "failed",
    "canceled": "cancelled",
    "cancel": "cancelled",
    "processing": "running",
    "queued": "pending",
    "等待中": "pending",
    "处理中": "running",
    "成功": "success",
    "失败": "failed",
    "partial": "partial",
    "部分失败": "partial",
    "取消": "cancelled",
}
_STATS_STATUS_LABELS: Dict[str, str] = {
    "success": "成功",
    "failed": "失败",
    "partial": "部分失败",
    "cancelled": "已取消",
    "running": "进行中",
    "pending": "排队中",
    "unknown": "未知",
}


def _stats_count(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return max(0, int(float(value)))
    except (OverflowError, TypeError, ValueError):
        return default


def _stats_status(entry: Dict[str, Any]) -> str:
    raw = _text(entry.get("status") or entry.get("state") or "").lower()
    if raw:
        return _STATS_STATUS_ALIASES.get(raw, raw if raw in _STATS_STATUS_ORDER else "unknown")
    if entry.get("ok") is True or entry.get("success") is True:
        return "success"
    if entry.get("ok") is False or entry.get("failed") is True or entry.get("error"):
        return "failed"
    return "unknown"


def _stats_entries(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _normalize_stats_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    status = _stats_status(entry)
    timestamp = _text(
        entry.get("time")
        or entry.get("timestamp")
        or entry.get("created_at")
        or entry.get("started_at")
    )
    seconds_value = entry.get("duration_seconds", entry.get("seconds", entry.get("elapsed_seconds", 0)))
    try:
        seconds = round(max(0.0, float(seconds_value)), 2)
    except (OverflowError, TypeError, ValueError):
        seconds = 0.0
    result = {
        "session_id": _text(
            entry.get("session_id") or entry.get("request_id") or entry.get("task_id") or entry.get("id")
        ),
        "status": status,
        "status_label": _STATS_STATUS_LABELS[status],
        "ok": True if status == "success" else (False if status == "failed" else None),
        "time": timestamp,
        "started_at": _text(entry.get("started_at")),
        "finished_at": _text(entry.get("finished_at") or entry.get("completed_at")),
        "seconds": seconds,
        "duration_seconds": seconds,
        "kind": _text(entry.get("kind") or entry.get("type")),
        "model": _text(entry.get("model")),
        "protocol": _text(entry.get("protocol")),
        "supplier": _text(entry.get("supplier") or entry.get("provider")),
        "images": _stats_count(entry.get("images", entry.get("image_count", 0))),
        "error": _text(entry.get("error") or entry.get("failure_reason") or entry.get("message")),
    }
    return result


def _stats_status_counts(raw: Dict[str, Any], totals: Dict[str, Any], entries: List[Dict[str, Any]]) -> Dict[str, int]:
    source = raw.get("status_counts")
    if not isinstance(source, dict):
        source = raw.get("by_status")
    counts = {status: 0 for status in _STATS_STATUS_ORDER}
    counts["partial"] = 0
    if isinstance(source, dict):
        for key, value in source.items():
            normalized = _STATS_STATUS_ALIASES.get(_text(key).lower(), _text(key).lower())
            if normalized in counts:
                counts[normalized] += _stats_count(value)

    has_cumulative_statuses = isinstance(source, dict)
    if not has_cumulative_statuses:
        for status in _STATS_STATUS_ORDER:
            if status in totals:
                counts[status] = _stats_count(totals.get(status))
        counts["partial"] = _stats_count(totals.get("partial"))
        counts["success"] = _stats_count(
            totals.get("success", raw.get("success_count", counts["success"]))
        )
        counts["failed"] = _stats_count(
            totals.get("failed", totals.get("failure", raw.get("failed_count", counts["failed"])))
        )
        if not any(counts.values()) and entries:
            for entry in entries:
                counts[_stats_status(entry)] += 1
    return counts


def _normalize_stats_snapshot(value: Dict[str, Any]) -> Dict[str, Any]:
    """统一旧版统计快照，给 /stats 提供可直接展示的会话维度数据。"""
    raw = dict(value)
    raw_totals = raw.get("totals")
    totals = dict(raw_totals) if isinstance(raw_totals, dict) else {}
    raw_entries = _stats_entries(raw.get("recent_sessions"))
    if not raw_entries:
        raw_entries = _stats_entries(raw.get("sessions"))
    if not raw_entries:
        raw_entries = _stats_entries(raw.get("recent"))
    recent_sessions = [_normalize_stats_entry(entry) for entry in raw_entries]

    counts = _stats_status_counts(raw, totals, raw_entries)
    existing_total = totals.get("total", raw.get("total"))
    total = _stats_count(existing_total, sum(counts.values()))
    if existing_total is None:
        total = sum(counts.values())
    totals.setdefault("success", counts["success"])
    totals.setdefault("failed", counts["failed"])
    totals.setdefault("total", total)
    totals["session_success"] = counts["success"]
    totals["session_failed"] = counts["failed"]
    totals["session_cancelled"] = counts["cancelled"]
    totals["session_partial"] = counts["partial"]

    result = dict(raw)
    result["totals"] = totals
    result["status_counts"] = dict(counts)
    result["by_status"] = dict(counts)
    result["session_totals"] = {
        "total": sum(counts.values()),
        "success": counts["success"],
        "failed": counts["failed"],
        "partial": counts["partial"],
        "cancelled": counts["cancelled"],
        "running": counts["running"],
        "pending": counts["pending"],
        "unknown": counts["unknown"],
    }
    result["session_success"] = counts["success"]
    result["session_failed"] = counts["failed"]
    result["recent_sessions"] = recent_sessions
    result["aggregates"] = {"by_status": dict(counts)}
    return result


def _stats_snapshot(plugin: Any) -> Dict[str, Any]:
    stats = getattr(plugin, "stats", None)
    if stats is not None:
        getter = getattr(stats, "snapshot", None)
        if callable(getter):
            try:
                value = getter()
            except Exception:  # noqa: BLE001
                value = None
            if isinstance(value, dict):
                return _normalize_stats_snapshot(value)
    return _normalize_stats_snapshot(_empty_stats())


def _sizes_response(cfg: Any) -> Any:
    sizes = _call_list(cfg, "image_sizes")
    return json_response(
        {"ok": True, "data": {"sizes": sizes, "active": _call_text(cfg, "active_size")}}
    )


def _suppliers_public(cfg: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    raw_by_name = _raw_supplier_by_name(cfg)
    for index, channel in enumerate(_existing_suppliers(cfg), start=1):
        extra = dict(channel.extra or {})
        raw = raw_by_name.get(channel.name.casefold(), {})
        safe_config = _public_value(raw) if raw else {}
        if not isinstance(safe_config, dict):
            safe_config = {}
        public_extra: Dict[str, Any] = {}
        for key, value in {**extra, **raw}.items():
            safe_value = _public_value(value, str(key))
            if safe_value is not None:
                public_extra[str(key)] = safe_value
        raw_protocol = normalize_protocol(
            raw.get("protocol")
            or raw.get("type")
            or raw.get("provider")
            or raw.get("__template_key")
            or ""
        )
        protocol = raw_protocol or _text(channel.protocol)
        model = _text(raw.get("model")) or _text(channel.model)
        edit_model = _text(raw.get("edit_model") or raw.get("editModel")) or _text(channel.edit_model)
        items.append(
            {
                "index": index,
                "name": channel.name,
                "base_url": _safe_url(channel.base_url),
                "base_url_configured": bool(_text(channel.base_url)),
                "protocol": protocol,
                "protocol_label": PROTOCOL_SHORT_LABELS.get(protocol, protocol),
                "model": model,
                "edit_model": edit_model,
                "api_key_set": bool(_text(channel.api_key)),
                "api_key_masked": _mask_key(channel.api_key),
                "protocols": list(PROTOCOL_KEYS),
                "options": public_extra,
                "config": safe_config,
            }
        )
    return items


def _model_page_fields(models: Sequence[str], page: int = 1, page_size: int = 0) -> Dict[str, Any]:
    """返回模型列表及分页元数据；未指定分页时保持旧 API 的全量返回行为。"""
    values = _dedupe(models)
    if page_size <= 0:
        return {
            "models": values,
            "page": 1,
            "page_size": len(values),
            "total": len(values),
            "total_pages": 1 if values else 0,
            "has_next": False,
            "has_previous": False,
        }
    total = len(values)
    total_pages = (total + page_size - 1) // page_size if total else 0
    current = max(1, min(page, total_pages or 1))
    start = (current - 1) * page_size
    selected = values[start : start + page_size]
    return {
        "models": selected,
        "page": current,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "has_next": current < total_pages,
        "has_previous": current > 1,
    }

# ------------------------------------------------------------------- 各 handler
async def _api_get_config(plugin: Any) -> Any:
    cfg = plugin.cfg
    data = cfg.to_public_dict()
    factory = PluginConfig({}).to_public_dict()
    data["defaults"] = {
        key: factory[key]
        for key in (
            "prompts", "gacha_styles", "flavor_lines", "menu_sections",
            "translate_prompt_text", "start_prompt", "done_prompt", "menu_text",
            "master_commands", "image_sizes", "translate_model",
            "translate_supplier", "img2prompt_model",
        )
    }
    return json_response({"ok": True, "data": data})


async def _api_save_config(plugin: Any) -> Any:
    cfg = plugin.cfg
    payload = await request.json(default={})
    if not isinstance(payload, dict):
        return error_response("请求体必须是 JSON 对象")
    try:
        plan = _build_plan(cfg, payload)
    except _ValidationError as exc:
        return error_response(str(exc))
    snapshot = _snapshot_config(cfg)
    try:
        _apply_plan(cfg, plan)
    except Exception as exc:  # noqa: BLE001 - 应用失败也必须恢复内存配置
        _restore_config(snapshot, cfg)
        return error_response("配置应用失败：%s" % exc)
    try:
        saved = cfg.save()
    except Exception as exc:  # noqa: BLE001 - 保存失败应返回可读的 API 错误
        _restore_config(snapshot, cfg)
        return error_response("配置保存失败：%s" % exc)
    if saved is False:
        _restore_config(snapshot, cfg)
        return error_response("配置保存失败：存储层未确认保存成功")
    reloaded = False
    try:
        reload_config = getattr(cfg, "reload", None)
        if callable(reload_config):
            reload_config()
            reloaded = True
    except Exception:
        reloaded = False
    return json_response({"ok": True, "data": {"saved": True, "reloaded": reloaded}})


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.search(r"\d+(?:\.\d+)+", str(value or ""))
    if not match:
        return (0,)
    return tuple(int(part) for part in match.group(0).split("."))


def _extract_changelog(text: str, version: str) -> str:
    if not text:
        return ""
    escaped = re.escape(version.lstrip("vV"))
    heading = r"^[ \t]*#{1,6}[ \t]*(?:v)?\d+(?:\.\d+)+(?:[ \t]*[-\u2013\u2014:][ \t]*[^\r\n]*)?$"
    pattern = re.compile(
        rf"(?ims)^[ \t]*#{{1,6}}[ \t]*(?:v)?{escaped}(?:[ \t]*[-\u2013\u2014:][ \t]*[^\r\n]*)?$.*?(?={heading}|\Z)"
    )
    match = pattern.search(text)
    if match:
        return match.group(0).strip()
    return text[:4000].strip()


async def _fetch_text(url: str) -> str:
    def fetch() -> str:
        request_obj = UrlRequest(url, headers={"User-Agent": "astrbot-plugin-gpt-image/1.0.8"})
        with urlopen(request_obj, timeout=8) as response:
            return response.read().decode("utf-8-sig", errors="replace")
    return await asyncio.to_thread(fetch)


async def _api_update_check(plugin: Any) -> Any:
    current = _read_version()
    try:
        changelog = await _fetch_text(UPDATE_CHANGELOG_URL)
        versions = re.findall(
            r"(?im)^[ \t]*#{1,6}[ \t]*v?(\d+(?:\.\d+)+)(?:[ \t]*[-\u2013\u2014:][ \t]*[^\r\n]*)?$",
            changelog,
        )
        latest = max(versions, key=_version_tuple) if versions else current
        entry = _extract_changelog(changelog, latest)
        return json_response({"ok": True, "data": {
            "current_version": current,
            "latest_version": latest,
            "update_available": _version_tuple(latest) > _version_tuple(current),
            "message": "发现新版本 v%s" % latest if _version_tuple(latest) > _version_tuple(current) else "当前已是最新版本（v%s）" % current,
            "changelog": entry,
            "download_url": UPDATE_BASE_URL + "/%s.zip" % latest,
            "changelog_url": UPDATE_CHANGELOG_URL,
        }})
    except Exception as exc:
        return json_response({"ok": False, "data": {
            "current_version": current,
            "latest_version": current,
            "update_available": False,
            "message": "检查更新失败，请检查网络或更新地址",
            "error": str(exc)[:240],
            "changelog_url": UPDATE_CHANGELOG_URL,
        }})


async def _api_suppliers(plugin: Any) -> Any:
    cfg = plugin.cfg
    suppliers = _suppliers_public(cfg)
    names = _dedupe([item.get("name") for item in suppliers] + _supplier_names(cfg))
    active = _call_text(cfg, "active_supplier_name") or _call_text(cfg, "active_channel_name")
    if active not in names and names:
        active = names[0]
    return json_response(
        {
            "ok": True,
            "data": {
                "suppliers": suppliers,
                "providers": suppliers,
                "channels": suppliers,
                "active": active,
                "active_index": (names.index(active) + 1) if active in names else 0,
                "total": len(names),
                "sources": ["suppliers", "providers", "channels", "nested.suppliers"],
                "protocols": _protocol_options(),
                "protocol_models": _protocol_models_public(cfg),
            },
        }
    )


def _diagnostic_stage(
    key: str,
    ok: bool,
    message: str,
    *,
    status: str = "passed",
    http_status: Optional[int] = None,
    detail: str = "",
) -> Dict[str, Any]:
    """生成稳定的分阶段诊断结构，供新版和旧版 UI 共同使用。"""
    result: Dict[str, Any] = {
        "key": key,
        "name": key,
        "ok": bool(ok),
        "status": status if status in ("passed", "failed", "warning", "skipped") else ("passed" if ok else "failed"),
        "message": _text(message),
    }
    if http_status is not None:
        result["http_status"] = http_status
    if detail:
        result["detail"] = detail
    return result


def _diagnostic_payload(payload: Any) -> Dict[str, Any]:
    """兼容 POST JSON 与 GET query，避免不同 Dashboard 调用方式产生差异。"""
    result = dict(payload) if isinstance(payload, dict) else {}
    query = getattr(request, "query", None)
    if query is not None and hasattr(query, "get"):
        for key in ("supplier", "channel", "name", "protocol", "refresh", "include_all"):
            if not result.get(key):
                value = query.get(key)
                if value is not None:
                    result[key] = value
    return result


async def _api_diagnose_supplier(plugin: Any) -> Any:
    """分阶段诊断供应商：URL 配置、鉴权、协议能力和模型目录。"""
    cfg = plugin.cfg
    try:
        payload = await request.json(default={})
    except Exception:  # noqa: BLE001 - GET 或旧版 request 对象可能没有 JSON body
        payload = {}
    payload = _diagnostic_payload(payload)
    name = _text(payload.get("supplier")) or _text(payload.get("channel")) or _text(payload.get("name"))
    if not name:
        return error_response("缺少参数 supplier")
    requested_protocol = _text(payload.get("protocol"))
    protocol = normalize_protocol(requested_protocol) or normalize_protocol(
        _call_text(cfg, "active_protocol")
    ) or PROTOCOL_KEYS[0]
    if requested_protocol and not normalize_protocol(requested_protocol):
        return error_response("不支持的协议：%s" % requested_protocol)

    channel = _protocol_model(plugin, cfg, name, protocol)
    if channel is None:
        return json_response(
            {
                "ok": False,
                "data": {
                    "ok": False,
                    "supplier": name,
                    "protocol": protocol,
                    "stages": [
                        _diagnostic_stage("url", False, "供应商不存在或未读取到完整配置", status="failed"),
                        _diagnostic_stage("authentication", False, "因供应商不存在而跳过", status="skipped"),
                        _diagnostic_stage("protocol", False, "因供应商不存在而跳过", status="skipped"),
                        _diagnostic_stage("models", False, "因供应商不存在而跳过", status="skipped"),
                    ],
                    "checks": {},
                    "models": [],
                    "models_count": 0,
                    "message": "供应商不存在：%s" % name,
                },
            }
        )

    base_url = _text(channel.base_url)
    try:
        parsed_url = urlsplit(base_url)
        url_valid = bool(
            base_url
            and parsed_url.scheme.lower() in ("http", "https")
            and bool(parsed_url.hostname)
            and not parsed_url.username
        )
    except Exception:  # noqa: BLE001
        url_valid = False
    if not url_valid:
        stages = [
            _diagnostic_stage("url", False, "接口地址必须是 http(s) URL，且不能包含 URL 用户凭据", status="failed"),
            _diagnostic_stage("authentication", False, "因 URL 无效而跳过", status="skipped"),
            _diagnostic_stage("protocol", False, "因 URL 无效而跳过", status="skipped"),
            _diagnostic_stage("models", False, "因 URL 无效而跳过", status="skipped"),
        ]
        return json_response(
            {
                "ok": False,
                "data": {
                    "ok": False,
                    "supplier": channel.name,
                    "protocol": protocol,
                    "base_url": _safe_url(base_url),
                    "resolved_base_url": _safe_url(base_url),
                    "api_key_set": bool(_text(channel.api_key)),
                    "api_key_masked": _mask_key(channel.api_key),
                    "stages": stages,
                    "checks": {stage["key"]: stage for stage in stages},
                    "models": [],
                    "models_count": 0,
                    "message": stages[0]["message"],
                },
            }
        )

    protocol_obj = None
    started = time.monotonic()
    stages: List[Dict[str, Any]] = [
        _diagnostic_stage("url", True, "接口地址格式有效", status="passed")
    ]
    models: List[str] = []
    cached = _model_cache(cfg, channel, "all")
    used_cache = False
    try:
        try:
            protocol_obj = _create_protocol(channel, cfg.timeout(), _call_text(cfg, "proxy"))
        except Exception as exc:  # noqa: BLE001
            error = _safe_error(exc)
            stages.extend(
                [
                    _diagnostic_stage("authentication", False, "无法创建协议客户端", status="failed", detail=error["message"]),
                    _diagnostic_stage("protocol", False, "协议客户端初始化失败", status="failed", detail=error["message"]),
                    _diagnostic_stage("models", False, "因协议客户端失败而跳过", status="skipped"),
                ]
            )
            return json_response(
                {
                    "ok": False,
                    "data": {
                        "ok": False,
                        "supplier": channel.name,
                        "protocol": protocol,
                        "base_url": _safe_url(base_url),
                        "resolved_base_url": _safe_url(base_url),
                        "api_key_set": bool(_text(channel.api_key)),
                        "api_key_masked": _mask_key(channel.api_key),
                        "stages": stages,
                        "checks": {stage["key"]: stage for stage in stages},
                        "models": cached,
                        "models_count": len(cached),
                        "cached": bool(cached),
                        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                        "message": error["message"],
                    },
                }
            )

        # list_models 使用协议层真实请求；HTTP 401/403 能准确区分鉴权失败，
        # 404/405/501 则会标记为协议或模型目录不兼容，而不是笼统报网络错误。
        supports_full_catalog = protocol in ("openai", "gemini", "grok", "flux")
        try:
            if supports_full_catalog:
                models = _dedupe(await protocol_obj.list_models(include_non_image=True))
            else:
                models = _dedupe(await protocol_obj.list_models())
        except Exception as exc:  # noqa: BLE001
            error = _safe_error(exc)
            http_status = error["http_status"]
            if http_status in (401, 403):
                auth_ok = False
                auth_status = "failed"
                auth_message = "鉴权失败，请检查 API Key 或供应商权限"
            else:
                auth_ok = True
                auth_status = "passed"
                auth_message = "未发现鉴权拒绝响应"
            protocol_ok = http_status not in (404, 405, 501)
            protocol_message = "协议请求已发送，但模型目录接口不可用" if not protocol_ok else "协议请求失败，请查看模型目录错误"
            stages.extend(
                [
                    _diagnostic_stage("authentication", auth_ok, auth_message, status=auth_status, http_status=http_status, detail=error["detail"]),
                    _diagnostic_stage("protocol", protocol_ok, protocol_message, status="passed" if protocol_ok else "failed", http_status=http_status, detail=error["message"]),
                    _diagnostic_stage("models", False, "模型目录获取失败：%s" % error["message"], status="failed", http_status=http_status, detail=error["detail"]),
                ]
            )
            models = cached
            used_cache = bool(cached)
            overall_ok = False
            message = error["message"]
        else:
            models = _dedupe(models)
            if models:
                _set_model_cache(cfg, channel, "all", models)
                cached = models
                model_stage = _diagnostic_stage("models", True, "成功获取 %d 个模型" % len(models), status="passed")
            else:
                models = cached
                used_cache = bool(cached)
                model_stage = _diagnostic_stage(
                    "models", False, "接口可达，但没有返回模型目录", status="warning"
                )
            stages.extend(
                [
                    _diagnostic_stage("authentication", True, "鉴权请求已通过", status="passed"),
                    _diagnostic_stage("protocol", True, "协议请求和响应解析正常", status="passed"),
                    model_stage,
                ]
            )
            overall_ok = bool(models) and all(stage["ok"] for stage in stages)
            message = "诊断通过，获取 %d 个模型" % len(models) if overall_ok else "接口可达，但模型目录为空"
    finally:
        if protocol_obj is not None:
            try:
                await protocol_obj.close()
            except Exception:  # noqa: BLE001
                pass

    resolved = base_url
    if protocol_obj is not None:
        getter = getattr(protocol_obj, "resolved_base_url", None)
        if callable(getter):
            try:
                resolved = _text(getter()) or base_url
            except Exception:  # noqa: BLE001
                resolved = base_url
    return json_response(
        {
            "ok": overall_ok,
            "data": {
                "ok": overall_ok,
                "supplier": channel.name,
                "protocol": protocol,
                "base_url": _safe_url(base_url),
                "resolved_base_url": _safe_url(resolved),
                "api_key_set": bool(_text(channel.api_key)),
                "api_key_masked": _mask_key(channel.api_key),
                "stages": stages,
                "checks": {stage["key"]: stage for stage in stages},
                "models": models,
                "models_count": len(models),
                "cached": used_cache,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "message": message,
            },
        }
    )


async def _api_test_supplier(plugin: Any) -> Any:
    cfg = plugin.cfg
    payload = await request.json(default={})
    if not isinstance(payload, dict):
        return error_response("请求体必须是 JSON 对象")
    name = _text(payload.get("supplier")) or _text(payload.get("channel")) or _text(payload.get("name"))
    if not name:
        return error_response("缺少参数 supplier")
    protocol = normalize_protocol(_text(payload.get("protocol"))) or normalize_protocol(
        _call_text(cfg, "active_protocol")
    )
    if not protocol:
        protocol = PROTOCOL_KEYS[0]

    channel = _protocol_model(plugin, cfg, name, protocol)
    if channel is None:
        return error_response("供应商不存在：%s" % name)
    if not _text(channel.base_url):
        return error_response("供应商「%s」还没有填写接口地址（Base URL）" % name)

    protocol_obj = _create_protocol(channel, cfg.timeout(), _call_text(cfg, "proxy"))
    result: Any = None
    try:
        try:
            result = await protocol_obj.test()
        except Exception as exc:  # noqa: BLE001 - 测试失败也走 200 + ok=false
            return json_response(
                {
                    "ok": False,
                    "data": {
                        "ok": False,
                        "supplier": channel.name,
                        "protocol": protocol,
                        "model": channel.generate_model,
                        "message": "%s: %s" % (type(exc).__name__, exc),
                    },
                }
            )
    finally:
        try:
            await protocol_obj.close()
        except Exception:  # noqa: BLE001
            pass

    if isinstance(result, tuple) and len(result) >= 2:
        ok = bool(result[0])
        message = str(result[1])
    else:
        ok = bool(result)
        message = ""
    return json_response(
        {
            "ok": ok,
            "data": {
                "ok": ok,
                "supplier": channel.name,
                "protocol": protocol,
                "model": channel.generate_model,
                "message": message,
            },
        }
    )


async def _api_protocols(plugin: Any) -> Any:
    cfg = plugin.cfg
    active = normalize_protocol(_call_text(cfg, "active_protocol")) or PROTOCOL_KEYS[0]
    items: List[Dict[str, Any]] = []
    for index, key in enumerate(PROTOCOL_KEYS, start=1):
        models = _models_for(cfg, key)
        items.append(
            {
                "key": key,
                "index": index,
                "label": PROTOCOL_SHORT_LABELS.get(key, key),
                "description": PROTOCOL_LABELS.get(key, key),
                "default_base_url": DEFAULT_BASE_URLS.get(key, ""),
                "model": _call_text(cfg, "model_for", key) or default_model_for(key),
                "edit_model": _call_text(cfg, "edit_model_for", key),
                "models": models,
                "models_count": len(models),
                "active": key == active,
            }
        )
    return json_response(
        {
            "ok": True,
            "data": {
                "protocols": items,
                "active": active,
                "active_index": PROTOCOL_KEYS.index(active) + 1,
                "protocol_models": _protocol_models_public(cfg),
            },
        }
    )


async def _api_list_models(plugin: Any) -> Any:
    cfg = plugin.cfg
    supplier = (
        _text(request.query.get("supplier"))
        or _text(request.query.get("supplier_name"))
        or _text(request.query.get("provider"))
        or _text(request.query.get("channel"))
    )
    if not supplier:
        supplier = _call_text(cfg, "active_supplier_name") or _call_text(cfg, "active_channel_name")
    requested_protocol = _text(request.query.get("protocol")) or _text(request.query.get("type"))
    protocol = normalize_protocol(requested_protocol) or normalize_protocol(
        _call_text(cfg, "active_protocol")
    )
    if not protocol:
        protocol = PROTOCOL_KEYS[0]

    refresh = _text(request.query.get("refresh")).lower() in ("1", "true", "yes")
    include_all = _text(
        request.query.get("include_all") or request.query.get("full")
    ).lower() in ("1", "true", "yes", "on")
    purpose = _text(request.query.get("purpose") or request.query.get("model_type")).lower()
    requested_page = _int_value(request.query.get("page"))
    requested_page_size = _int_value(
        request.query.get("page_size") or request.query.get("limit")
    )
    page = max(1, requested_page or 1)
    page_size = max(1, min(requested_page_size, MODEL_CACHE_LIMIT)) if requested_page_size else 0
    requested_offset = _int_value(request.query.get("offset"))
    if requested_offset is not None and requested_offset > 0 and page_size:
        page = requested_offset // page_size + 1
    if purpose not in ("", "text", "vision") or (
        requested_protocol and not normalize_protocol(requested_protocol)
    ):
        return json_response(
            {
                "ok": False,
                "data": {
                    **_model_page_fields([], page, page_size),
                    "supplier": supplier,
                    "protocol": protocol,
                    "message": (
                        "purpose 只支持 text 或 vision"
                        if purpose not in ("", "text", "vision")
                        else "不支持的协议：%s" % requested_protocol
                    ),
                },
            }
        )
    full_directory = include_all or bool(purpose)
    scope = purpose or ("all" if full_directory else "image")
    channel = _protocol_model(plugin, cfg, supplier, protocol)
    cached = _model_cache(cfg, channel, scope) if channel is not None else []
    fallback: List[str] = []
    if not full_directory:
        for method in ("model_for", "edit_model_for"):
            value = _call_text(cfg, method, protocol)
            if value and value not in fallback:
                fallback.append(value)

    if channel is None:
        models = fallback
        return json_response(
            {
                "ok": False,
                "data": {
                    **_model_page_fields(models, page, page_size),
                    "supplier": supplier,
                    "protocol": protocol,
                    "cached": bool(cached),
                    "message": "供应商不存在：%s" % (supplier or "(空)"),
                },
            }
        )

    if cached and not refresh:
        return json_response(
            {
                "ok": True,
                "data": {
                    **_model_page_fields(
                        cached if full_directory else _dedupe(cached + fallback),
                        page,
                        page_size,
                    ),
                    "supplier": channel.name,
                    "protocol": protocol,
                    "cached": True,
                    "message": "已使用当前供应商的 %d 个模型缓存（可点「刷新」重新拉取）" % len(cached),
                },
            }
        )

    protocol_obj = None
    raw_models: Any = None
    try:
        if full_directory:
            protocol_obj = _create_protocol(
                channel,
                cfg.timeout(),
                _call_text(cfg, "proxy"),
                include_non_image=True,
            )
            raw_models = await protocol_obj.list_models(include_non_image=True)
        else:
            protocol_obj = _create_protocol(channel, cfg.timeout(), _call_text(cfg, "proxy"))
            raw_models = await protocol_obj.list_models()
        models = _model_names(raw_models)
    except Exception as exc:  # noqa: BLE001 - 前端需要展示错误信息
        models = _dedupe(cached + fallback)
        message = "%s: %s" % (type(exc).__name__, exc)
        if full_directory and getattr(exc, "status", None) in (404, 405, 501):
            message = "该供应商不支持当前协议的完整模型目录，请手动填写模型。%s" % message
        return json_response(
            {
                "ok": False,
                "data": {
                    **_model_page_fields(models, page, page_size),
                    "supplier": channel.name,
                    "protocol": protocol,
                    "cached": bool(cached),
                    "message": message,
                },
            }
        )
    finally:
        if protocol_obj is not None:
            try:
                await protocol_obj.close()
            except Exception:  # noqa: BLE001
                pass

    if not models:
        models = _dedupe(cached + fallback)
        return json_response(
            {
                "ok": False,
                "data": {
                    **_model_page_fields(models, page, page_size),
                    "supplier": channel.name,
                    "protocol": protocol,
                    "cached": bool(cached),
                    "message": "接口没有返回任何模型，请检查该供应商是否支持此协议",
                },
            }
        )

    merged = models if full_directory else _dedupe(models + cached + fallback)
    if merged != cached:
        _set_model_cache(cfg, channel, scope, merged)
    return json_response(
        {
            "ok": True,
            "data": {
                **_model_page_fields(merged, page, page_size),
                "supplier": channel.name,
                "protocol": protocol,
                "cached": False,
                "message": "已获取 %d 个模型" % len(merged),
            },
        }
    )

async def _api_list_platforms(plugin: Any) -> Any:
    platforms: List[Dict[str, str]] = []
    types: List[str] = []

    manager = getattr(plugin.context, "platform_manager", None)
    insts: List[Any] = []
    if manager is not None:
        try:
            insts = list(getattr(manager, "platform_insts", None) or [])
        except Exception:  # noqa: BLE001
            insts = []
        if not insts:
            getter = getattr(manager, "get_insts", None)
            if callable(getter):
                try:
                    insts = list(getter() or [])
                except Exception:  # noqa: BLE001
                    insts = []

    for inst in insts:
        try:
            meta = inst.meta()
        except Exception:  # noqa: BLE001
            continue
        if meta is None:
            continue
        name = str(getattr(meta, "name", "") or "")
        display = str(getattr(meta, "adapter_display_name", "") or "") or name
        platforms.append(
            {
                "id": str(getattr(meta, "id", "") or ""),
                "name": name,
                "display_name": display,
                "description": str(getattr(meta, "description", "") or ""),
            }
        )
        if name and name not in types:
            types.append(name)

    cfg = plugin.cfg
    try:
        selected_platforms = _call_list(cfg, "enabled_platforms")
        selected_bots = _call_list(cfg, "enabled_bot_ids")
    except Exception:  # noqa: BLE001
        selected_platforms, selected_bots = [], []

    return json_response(
        {
            "ok": True,
            "data": {
                "platforms": platforms,
                "types": types,
                "selected_platforms": selected_platforms,
                "selected_bot_ids": selected_bots,
            },
        }
    )


async def _api_recent(plugin: Any) -> Any:
    """返回插件最近见过的用户与群，供设置页「一键获取」使用。

    数据来自 ``Main`` 在处理消息时记录的内存快照（最多各 30 条，最近的在前）。
    只能看到「插件被唤醒过」的会话，因此需要先让目标用户 / 群与机器人交互一次。
    """
    users: List[Dict[str, str]] = []
    groups: List[Dict[str, str]] = []

    for attr, target in (("recent_users", users), ("recent_groups", groups)):
        raw = getattr(plugin, attr, None)
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            identifier = str(item.get("id") or "").strip()
            if not identifier:
                continue
            target.append(
                {
                    "id": identifier,
                    "name": str(item.get("name") or ""),
                    "platform": str(item.get("platform") or ""),
                    "platform_id": str(item.get("platform_id") or ""),
                    "time": str(item.get("time") or ""),
                }
            )

    return json_response({"ok": True, "data": {"users": users, "groups": groups}})


async def _api_status(plugin: Any) -> Any:
    cfg = plugin.cfg
    names = _supplier_names(cfg)
    active_supplier = _call_text(cfg, "active_supplier_name") or _call_text(cfg, "active_channel_name")
    protocol = normalize_protocol(_call_text(cfg, "active_protocol")) or PROTOCOL_KEYS[0]

    channel: Optional[Channel] = None
    try:
        channel = cfg.active_channel()
    except Exception:  # noqa: BLE001
        channel = None
    if not isinstance(channel, Channel):
        channel = _protocol_model(plugin, cfg, active_supplier, protocol)

    model = _text(getattr(channel, "generate_model", ""))
    if not model:
        model = _call_text(cfg, "model_for", protocol) or default_model_for(protocol)

    data = {
        "version": _read_version(),
        "active_supplier": active_supplier,
        "supplier_index": (names.index(active_supplier) + 1) if active_supplier in names else 0,
        "supplier_total": len(names),
        "active_provider": active_supplier,
        "protocol": protocol,
        "protocol_label": PROTOCOL_SHORT_LABELS.get(protocol, protocol),
        "protocol_index": PROTOCOL_KEYS.index(protocol) + 1 if protocol in PROTOCOL_KEYS else 0,
        "model": model,
        "edit_model": _call_text(cfg, "edit_model_for", protocol),
        "active_size": _call_text(cfg, "active_size"),
        "suppliers": names,
        "channels": names,
        "sizes": _call_list(cfg, "image_sizes"),
        "protocols": _protocol_options(),
        "protocol_options": _protocol_options(),
        "trigger_mode": _call_text(cfg, "trigger_mode") or "at",
        "cooldown": _call_number(cfg, "cooldown"),
        "retry_times": int(_call_number(cfg, "retry_times")),
        "proxy_set": bool(_call_text(cfg, "proxy")),
        "masters": _call_list(cfg, "masters"),
        "private_enabled": bool(cfg.private_enabled()),
        "group_mode": _call_text(cfg, "group_mode"),
        "stats": _stats_snapshot(plugin).get("totals", {}),
    }
    return json_response({"ok": True, "data": data})


async def _api_stats(plugin: Any) -> Any:
    return json_response({"ok": True, "data": _stats_snapshot(plugin)})


async def _api_get_sizes(plugin: Any) -> Any:
    return _sizes_response(plugin.cfg)


async def _api_add_size(plugin: Any) -> Any:
    cfg = plugin.cfg
    payload = await request.json(default={})
    if not isinstance(payload, dict):
        return error_response("请求体必须是 JSON 对象")
    text = _text(payload.get("size"))
    if not text:
        return error_response("缺少参数 size")
    normalized = normalize_size(text, "")
    if not normalized or not looks_like_size(normalized):
        return error_response("尺寸格式不正确，应形如 1024x1024 / 1536x1024 / auto")
    cfg.add_size(normalized)
    _sync_sizes(cfg)
    cfg.save()
    return _sizes_response(cfg)


async def _api_delete_size(plugin: Any) -> Any:
    cfg = plugin.cfg
    payload = await request.json(default={})
    if not isinstance(payload, dict):
        return error_response("请求体必须是 JSON 对象")
    text = _text(payload.get("size"))
    if not text:
        return error_response("缺少参数 size")
    cfg.remove_size(text)
    _sync_sizes(cfg)
    cfg.save()
    return _sizes_response(cfg)


# -------------------------------------------------------------------- 注册入口
def register_web_apis(plugin: Any) -> None:
    """注册设置界面所需的全部 Web API。

    :param plugin: ``Main`` 实例，需具备 ``context`` / ``cfg`` / ``state`` / ``logger``。
    """
    context = getattr(plugin, "context", None)
    register = getattr(context, "register_web_api", None)
    if not callable(register):
        try:
            plugin.logger.warning("[%s] 当前 AstrBot 不支持插件 Web API，跳过注册" % PLUGIN_NAME)
        except Exception:  # noqa: BLE001
            pass
        return

    def bind(func: Any, label: str) -> Any:
        """把 ``func(plugin, ...)`` 绑定为无参 handler，并加上异常兜底。"""

        @functools.wraps(func)
        async def handler(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(plugin, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 兜底，避免 Dashboard 500
                try:
                    plugin.logger.exception("[%s] %s 失败" % (PLUGIN_NAME, label))
                except Exception:  # noqa: BLE001
                    pass
                return error_response("%s: %s" % (type(exc).__name__, exc))

        return handler

    base = "/%s" % PLUGIN_NAME

    register(base + "/config", bind(_api_get_config, "GET /config"), ["GET"], "读取插件配置（不含密钥明文）")
    register(base + "/config", bind(_api_save_config, "POST /config"), ["POST"], "合并保存插件配置")
    register(base + "/suppliers", bind(_api_suppliers, "GET /suppliers"), ["GET"], "获取供应商列表")
    register(
        base + "/suppliers/test",
        bind(_api_test_supplier, "POST /suppliers/test"),
        ["POST"],
        "测试供应商在当前协议下的连通性",
    )
    register(
        base + "/suppliers/diagnose",
        bind(_api_diagnose_supplier, "POST /suppliers/diagnose"),
        ["GET", "POST"],
        "分阶段诊断供应商 URL、鉴权、协议和模型目录",
    )
    register(
        base + "/suppliers/diagnostic",
        bind(_api_diagnose_supplier, "POST /suppliers/diagnostic"),
        ["GET", "POST"],
        "供应商诊断兼容别名",
    )
    register(base + "/protocols", bind(_api_protocols, "GET /protocols"), ["GET"], "获取协议选项")
    register(base + "/models", bind(_api_list_models, "GET /models"), ["GET"], "拉取模型列表")
    register(base + "/update-check", bind(_api_update_check, "GET /update-check"), ["GET"], "检查插件更新")
    register(
        base + "/platforms",
        bind(_api_list_platforms, "GET /platforms"),
        ["GET"],
        "获取平台适配器实例列表",
    )
    register(base + "/status", bind(_api_status, "GET /status"), ["GET"], "获取插件运行状态")
    register(base + "/stats", bind(_api_stats, "GET /stats"), ["GET"], "获取运行统计")
    register(
        base + "/recent",
        bind(_api_recent, "GET /recent"),
        ["GET"],
        "获取最近交互过的用户与群（一键获取用）",
    )
    register(base + "/sizes", bind(_api_get_sizes, "GET /sizes"), ["GET"], "获取尺寸列表")
    register(base + "/sizes/add", bind(_api_add_size, "POST /sizes/add"), ["POST"], "添加尺寸")
    register(base + "/sizes/delete", bind(_api_delete_size, "POST /sizes/delete"), ["POST"], "删除尺寸")


__all__ = ["PLUGIN_NAME", "register_web_apis"]
