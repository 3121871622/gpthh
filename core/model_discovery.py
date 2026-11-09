"""只读模型目录发现：精确匹配、短超时、有界分页和明确缓存来源。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import time
from typing import Any
from weakref import WeakKeyDictionary

MODEL_CHECK_TIMEOUT = 12.0
MODEL_CHECK_CACHE_TTL = 30.0
MODEL_CHECK_MAX_PAGES = 10
MODEL_CHECK_MAX_CONCURRENT = 3
_SEMAPHORES: WeakKeyDictionary = WeakKeyDictionary()
_OPENAI_CATALOGS = frozenset({"openai", "grok", "flux", "jimeng"})
_ID_FIELDS = ("id", "model_id", "modelId", "slug", "name", "model", "model_name", "modelName")
_LIST_FIELDS = ("data", "models", "items", "results", "list", "result", "response")


@dataclass(frozen=True)
class Catalog:
    models: tuple[str, ...]
    complete: bool
    note: str = ""
    online: bool = True
    http_status: int | None = None


def result_fields(supplier: str, protocol: str, model: str, purpose: str) -> dict[str, Any]:
    return {
        "ok": False, "supplier": supplier, "protocol": protocol, "model": model,
        "purpose": purpose, "status": "invalid", "connection": "unknown", "matched": None,
        "match_type": "none", "canonical_model": "", "candidates": [],
        "catalog_total": 0, "catalog_complete": False, "cached": False,
        "checked_at": "", "elapsed_ms": 0, "http_status": None, "message": "",
        "generation_verified": False,
    }


def _entries(payload: Any, depth: int = 0) -> list | None:
    if isinstance(payload, list): return payload
    if not isinstance(payload, dict) or depth > 5: return None
    for key in _LIST_FIELDS:
        value = payload.get(key)
        if isinstance(value, (dict, list)):
            found = _entries(value, depth + 1)
            if found is not None: return found
    return None


def _ids(entries: list, fields: tuple[str, ...] = _ID_FIELDS) -> list[str]:
    names = []
    for entry in entries:
        if isinstance(entry, str): name = entry.strip()
        elif isinstance(entry, dict):
            name = next((entry[key].strip() for key in fields if isinstance(entry.get(key), str) and entry[key].strip()), "")
        else: continue
        if name: names.append(name)
    return list(dict.fromkeys(names))


def _pagination_unknown(payload: Any, count: int) -> bool:
    if not isinstance(payload, dict): return False
    # 不猜测未知供应商的游标含义，更不能把首屏目录误判成完整目录。
    for key in ("next", "next_page", "nextPage", "next_cursor", "nextCursor", "nextPageToken",
                "next_page_token", "nextToken", "pagination", "links", "has_next", "hasNextPage"):
        if payload.get(key) not in (None, "", False, [], {}): return True
    if not isinstance(payload.get("has_more"), bool):
        for key in ("page", "page_size", "pageSize", "per_page", "offset", "limit", "total_pages", "totalPages"):
            if payload.get(key) not in (None, "", False): return True
    for key in (*_LIST_FIELDS, "meta", "metadata"):
        nested = payload.get(key)
        if isinstance(nested, dict) and (nested.get("has_more") is True or _pagination_unknown(nested, count)):
            return True
    for key in ("total", "total_count", "totalCount"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > count: return True
    if "has_more" in payload and not isinstance(payload["has_more"], bool): return True
    return False


def _response_status(response: Any) -> int | None:
    status = getattr(response, "status", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _catalog_error(payload: Any, depth: int = 0) -> bool:
    if not isinstance(payload, dict) or depth > 5: return False
    if payload.get("error") or payload.get("errors") or str(payload.get("status", "")).lower() in ("error", "failed", "failure"):
        return True
    return any(_catalog_error(payload.get(key), depth + 1) for key in _LIST_FIELDS if isinstance(payload.get(key), dict))


async def _paged_catalog(client: Any, channel: Any, *, gemini: bool = False) -> Catalog:
    from .assist import _headers_for, api_url

    url = api_url(channel.base_url, channel.protocol, "models")
    headers = _headers_for(channel, gemini=True) if gemini else _headers_for(channel)
    fields = ("name", "id") if gemini else _ID_FIELDS
    if gemini:
        from .protocols.gemini import GeminiProtocol
    models: list[str] = []
    seen_cursors: set[str] = set()
    params = None
    status = None
    for _page in range(MODEL_CHECK_MAX_PAGES):
        response = await client._request("GET", url, headers=headers, params=params)
        status = _response_status(response)
        response.ensure_ok("读取模型目录")
        payload = response.data
        entries = _entries(payload)
        if entries is None or _catalog_error(payload) or any(not _ids([item], fields) for item in entries):
            return Catalog(tuple(models), False, "供应商响应不包含可确认的模型目录", http_status=status)
        names = _ids(entries, fields)
        if gemini:
            names = [GeminiProtocol._strip_model_prefix(name) for name in names]
        models = list(dict.fromkeys([*models, *(name for name in names if name)]))
        metadata = dict(payload) if isinstance(payload, dict) else {}
        if gemini:
            cursor = metadata.pop("nextPageToken", None)
            if cursor not in (None, "") and not isinstance(cursor, str):
                return Catalog(tuple(models), False, "模型目录返回了无效分页标记", http_status=status)
            more = bool(cursor)
            if metadata.get("has_more") is True:
                return Catalog(tuple(models), False, "目录包含未识别的分页标记，尚未读取完整目录", http_status=status)
        else:
            more = metadata.get("has_more") is True
            cursor = metadata.get("last_id") if more else None
        if _pagination_unknown(metadata, len(models)):
            # 原生分页进行中时，total 大于当前累计数量不代表分页格式未知。
            without_totals = {key: value for key, value in metadata.items() if key not in ("total", "total_count", "totalCount")}
            if not more or _pagination_unknown(without_totals, len(models)):
                return Catalog(tuple(models), False, "目录包含未识别的分页标记，尚未读取完整目录", http_status=status)
        if not more:
            return Catalog(tuple(models), True, http_status=status)
        if not isinstance(cursor, str) or not cursor.strip():
            return Catalog(tuple(models), False, "目录声明还有下一页，但没有提供可识别的游标", http_status=status)
        if cursor in seen_cursors:
            return Catalog(tuple(models), False, "模型目录分页游标重复，已停止重复请求", http_status=status)
        seen_cursors.add(cursor)
        params = {"pageToken" if gemini else "after": cursor}
    return Catalog(tuple(models), False, "已达到模型目录分页上限，尚未确认完整目录", http_status=status)


async def _read_catalog(client: Any, channel: Any) -> Catalog:
    protocol = channel.protocol
    if protocol in _OPENAI_CATALOGS:
        return await _paged_catalog(client, channel)
    if protocol == "gemini":
        return await _paged_catalog(client, channel, gemini=True)
    if protocol == "sdwebui":
        response = await client._request("GET", client._url("sdapi/v1/sd-models"))
        response.ensure_ok("读取模型目录")
        entries = _entries(response.data)
        return Catalog(tuple(_ids(entries or [], ("title", "model_name", "name", "filename"))), False,
                       "只核对本次返回的 checkpoint 目录；未找到时仍需人工确认", http_status=_response_status(response))
    if protocol == "comfyui":
        response = await client._request("GET", client._url("object_info/CheckpointLoaderSimple"))
        response.ensure_ok("读取模型目录")
        payload = response.data if isinstance(response.data, dict) else {}
        node = payload.get("CheckpointLoaderSimple") or {}
        required = node.get("input", {}).get("required", {}) if isinstance(node, dict) else {}
        values = required.get("ckpt_name") if isinstance(required, dict) else None
        names = _ids(values[0]) if isinstance(values, list) and values and isinstance(values[0], list) else []
        return Catalog(tuple(names), False, "只核对本次返回的 checkpoint 目录；未找到时仍需人工确认", http_status=_response_status(response))
    return Catalog((), False, "当前协议未提供可用于自动核对的统一模型目录", online=False)


def _cache_key(channel: Any, proxy: str) -> tuple[str, ...]:
    return (channel.name, channel.base_url, hashlib.sha256(str(channel.api_key or "").encode()).hexdigest(), channel.protocol, proxy)


def _apply_catalog(result: dict[str, Any], catalog: Catalog, *, cached: bool, checked_at: str) -> None:
    result.update(catalog_total=len(catalog.models), catalog_complete=catalog.complete,
                  cached=cached, checked_at=checked_at,
                  connection="cached" if cached else "online" if catalog.online else "unknown",
                  http_status=catalog.http_status if catalog.online and not cached else None)
    requested = result["model"]
    if requested in catalog.models:
        result.update(ok=True, status="matched", matched=True, match_type="exact", canonical_model=requested,
                      message="模型 ID 与%s目录精确匹配；未执行出图验证" % ("缓存" if cached else "在线返回"))
        return
    suggestions = [name for name in catalog.models if name.casefold() == requested.casefold()][:5]
    result["candidates"] = suggestions
    if not catalog.complete:
        result.update(status="catalog_unavailable", message=catalog.note or "未在返回目录找到该模型，目录完整性尚未确认")
    elif suggestions:
        result.update(ok=True, status="case_mismatch", matched=False, match_type="case_insensitive",
                      canonical_model=suggestions[0] if len(suggestions) == 1 else "",
                      message="只找到大小写不同的模型 ID，请核对后采用目录中的准确名称")
    else:
        result.update(ok=True, status="not_found", matched=False,
                      message="未在%s完整目录找到该模型；未执行生成请求" % ("缓存" if cached else "在线返回的"))


def _is_timeout(error: BaseException) -> bool:
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, (asyncio.TimeoutError, TimeoutError)): return True
        seen.add(id(error))
        error = error.__cause__ or error.__context__
    return False


def _failure(result: dict[str, Any], error: Exception) -> None:
    # 异常正文、响应正文和请求 URL 都可能带认证数据；只回传受控分类文案。
    status = getattr(error, "status", None)
    status = status if isinstance(status, int) and not isinstance(status, bool) else None
    result["http_status"] = status
    if _is_timeout(error):
        result.update(status="timeout", connection="timeout", message="模型目录检测超时；其他供应商会继续检测")
    elif status in (401, 403):
        result.update(status="auth_failed", connection="auth_failed", message="供应商拒绝访问模型目录，请检查已保存密钥及权限")
    elif status in (404, 405, 501):
        result.update(status="catalog_unavailable", connection="online", message="供应商未提供当前协议的模型目录，尚未确认模型支持情况")
    elif status == 429:
        result.update(status="catalog_unavailable", connection="online", message="供应商限制模型目录请求频率，请稍后重试")
    else:
        result.update(status="network_error", connection="online" if status else "unreachable",
                      message="供应商模型目录请求失败，请检查连接或稍后重试")


async def check_model(cfg: Any, channel: Any, model: str, purpose: str, *, factory: Any,
                      proxy: str = "", refresh: bool = False) -> dict[str, Any]:
    started = time.monotonic()
    result = result_fields(channel.name, channel.protocol, model, purpose)
    cache = getattr(cfg, "_model_discovery_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        cfg._model_discovery_cache = cache
    key = _cache_key(channel, proxy)
    entry = cache.get(key)
    if not refresh and entry and started - entry[0] < MODEL_CHECK_CACHE_TTL:
        _apply_catalog(result, entry[1], cached=True, checked_at=entry[2])
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
        return result
    # 刷新失败不能让旧目录在下一次自动检测中再次冒充成功结果。
    cache.pop(key, None)
    tokens = getattr(cfg, "_model_discovery_tokens", None)
    if not isinstance(tokens, dict):
        tokens = {}
        cfg._model_discovery_tokens = tokens
    token = object()
    tokens.pop(key, None)
    tokens[key] = token
    while len(tokens) > 128: tokens.pop(next(iter(tokens)))
    loop = asyncio.get_running_loop()
    semaphore = _SEMAPHORES.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(MODEL_CHECK_MAX_CONCURRENT)
        _SEMAPHORES[loop] = semaphore
    try:
        configured = float(cfg.timeout())
    except (AttributeError, TypeError, ValueError):
        configured = MODEL_CHECK_TIMEOUT
    budget = min(MODEL_CHECK_TIMEOUT, configured) if configured > 0 else MODEL_CHECK_TIMEOUT
    cleanup_budget = min(1.0, budget / 10)
    work_budget = budget - cleanup_budget

    async def fetch() -> Catalog:
        async with semaphore:
            if channel.protocol == "tongyi":
                return Catalog((), False, "当前协议未提供可用于自动核对的统一模型目录", online=False)
            client = factory(channel, work_budget, proxy)
            try:
                return await _read_catalog(client, channel)
            finally:
                try:
                    await asyncio.wait_for(client.close(), timeout=cleanup_budget)
                except (Exception, asyncio.CancelledError):
                    pass
    try:
        catalog = await asyncio.wait_for(fetch(), timeout=work_budget)
        checked_at = datetime.now(timezone.utc).isoformat()
        _apply_catalog(result, catalog, cached=False, checked_at=checked_at)
        if catalog.complete and catalog.online and tokens.get(key) is token:
            cache[key] = (time.monotonic(), catalog, checked_at)
            while len(cache) > 64: cache.pop(next(iter(cache)))
    except Exception as error:
        _failure(result, error)
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
    return result
