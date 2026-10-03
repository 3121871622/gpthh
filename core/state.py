"""会话级运行时状态（供应商 / 协议 / 模型 / 尺寸覆盖）。

每个会话（unified_msg_origin）可以独立覆盖：
``supplier``（供应商名）、``protocol``（协议 key）、``model``、``size``。
底层使用 AstrBot 的插件 KV 存储（``Star.put_kv_data`` / ``get_kv_data``），
读取失败时退化为进程内内存缓存，保证功能可用。
"""

from __future__ import annotations

from typing import Any

STORE_KEY = "session_overrides"

#: 允许被覆盖的字段（``provider`` 作为 ``supplier`` 的历史别名一并保留）
OVERRIDE_KEYS = ("supplier", "protocol", "model", "size")

#: 历史别名 -> 规范键
_ALIASES = {
    "provider": "supplier",
    "suppliers": "supplier",
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:  # noqa: BLE001
        return ""


class SessionState:
    """按 unified_msg_origin 保存的会话覆盖值。"""

    def __init__(self, star: Any) -> None:
        self._star = star
        self._data: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._dirty = False

    # ------------------------------------------------------------------ 存取
    async def load(self) -> None:
        if self._loaded:
            return
        raw: Any = None
        getter = getattr(self._star, "get_kv_data", None)
        if callable(getter):
            try:
                raw = await getter(STORE_KEY, {})
            except Exception:  # noqa: BLE001 - KV 不可用时退化为内存
                raw = None
        if isinstance(raw, dict):
            cleaned: dict[str, dict[str, Any]] = {}
            for umo, values in raw.items():
                if not isinstance(values, dict):
                    continue
                bucket: dict[str, Any] = {}
                for key, value in values.items():
                    canonical = _ALIASES.get(str(key), str(key))
                    if canonical not in OVERRIDE_KEYS:
                        continue
                    text = _text(value)
                    if text:
                        bucket[canonical] = text
                if bucket:
                    cleaned[str(umo)] = bucket
            self._data = cleaned
        self._loaded = True

    async def _flush(self) -> None:
        if not self._dirty:
            return
        setter = getattr(self._star, "put_kv_data", None)
        if callable(setter):
            try:
                await setter(STORE_KEY, self._data)
                self._dirty = False
            except Exception:  # noqa: BLE001
                return

    async def get(self, umo: str, key: str, default: Any = None) -> Any:
        await self.load()
        canonical = _ALIASES.get(str(key), str(key))
        return self._data.get(str(umo), {}).get(canonical, default)

    async def set(self, umo: str, key: str, value: Any) -> None:
        await self.load()
        canonical = _ALIASES.get(str(key), str(key))
        if canonical not in OVERRIDE_KEYS:
            return
        bucket = self._data.setdefault(str(umo), {})
        text = _text(value)
        if text:
            bucket[canonical] = text
        else:
            bucket.pop(canonical, None)
        if not bucket:
            self._data.pop(str(umo), None)
        self._dirty = True
        await self._flush()

    async def update(self, umo: str, **values: Any) -> None:
        for key, value in values.items():
            await self.set(umo, key, value)

    async def clear(self, umo: str) -> None:
        """清空某个会话的全部覆盖值（「重置设置」指令使用）。"""
        await self.load()
        if self._data.pop(str(umo), None) is not None:
            self._dirty = True
            await self._flush()

    async def snapshot(self, umo: str) -> dict[str, str]:
        await self.load()
        return dict(self._data.get(str(umo), {}))

    # ------------------------------------------------------------------ 解析
    async def resolved(self, umo: str, cfg: Any) -> dict[str, Any]:
        """解析出当前会话实际使用的供应商 / 协议 / 模型 / 尺寸。

        返回值里的 ``*_index`` 是 1-based 序号（供菜单与「切换 xxx <序号>」使用），
        取不到时统一为 0；``*_total`` 为对应列表长度。
        """
        overrides = await self.snapshot(umo)

        supplier_names = _call_list(cfg, "supplier_names")
        if not supplier_names:
            supplier_names = _call_list(cfg, "channel_names")
        protocols = _call_list(cfg, "protocols") or ["openai", "gemini", "grok"]

        supplier_name = overrides.get("supplier") or _call_text(cfg, "active_supplier_name")
        if not supplier_name:
            supplier_name = _call_text(cfg, "active_channel_name")
        if supplier_name and supplier_name not in supplier_names:
            supplier_name = supplier_names[0] if supplier_names else supplier_name
        if not supplier_name and supplier_names:
            supplier_name = supplier_names[0]

        protocol = overrides.get("protocol") or _call_text(cfg, "active_protocol") or protocols[0]
        if protocol not in protocols:
            protocol = protocols[0]

        model_override = _text(overrides.get("model"))

        channel = _build_channel(cfg, supplier_name, protocol, model_override)

        models = _call_list(cfg, "models_for", protocol)
        model = ""
        if channel is not None:
            model = _text(getattr(channel, "generate_model", ""))
        elif model_override:
            model = model_override
        if not model:
            model = model_override or _call_text(cfg, "model_for", protocol)

        size = overrides.get("size") or _call_text(cfg, "active_size")

        return {
            "channel": channel,
            "channel_name": supplier_name,
            "supplier": supplier_name,
            "supplier_name": supplier_name,
            "supplier_index": _index_of(supplier_names, supplier_name),
            "supplier_total": len(supplier_names),
            "protocol": protocol,
            "protocol_label": _protocol_label(protocol),
            "protocol_index": _index_of(protocols, protocol),
            "model": model,
            "edit_model": _text(getattr(channel, "edit_model_name", "")) if channel else "",
            "model_index": _index_of(models, model),
            "model_total": len(models),
            "size": size,
            "overrides": overrides,
        }


def _call_list(cfg: Any, method: str, *args: Any) -> list:
    getter = getattr(cfg, method, None)
    if not callable(getter):
        return []
    try:
        value = getter(*args)
    except Exception:  # noqa: BLE001 - 配置异常不应影响渲染
        return []
    if isinstance(value, (list, tuple)):
        return [item for item in value if _text(item)]
    return []


def _call_text(cfg: Any, method: str, *args: Any) -> str:
    getter = getattr(cfg, method, None)
    if not callable(getter):
        return ""
    try:
        return _text(getter(*args))
    except Exception:  # noqa: BLE001
        return ""


def _build_channel(cfg: Any, supplier: str, protocol: str, model: str) -> Any:
    builder = getattr(cfg, "build_channel", None)
    if callable(builder):
        try:
            return builder(supplier=supplier, protocol=protocol, model=model)
        except Exception:  # noqa: BLE001
            return None
    # 兼容旧 PluginConfig
    legacy = getattr(cfg, "get_channel", None)
    if callable(legacy) and supplier:
        try:
            return legacy(supplier)
        except Exception:  # noqa: BLE001
            return None
    return None


def _index_of(items: list, value: str) -> int:
    target = _text(value)
    if not target:
        return 0
    for index, item in enumerate(items, start=1):
        if _text(item) == target:
            return index
    lowered = target.lower()
    for index, item in enumerate(items, start=1):
        if _text(item).lower() == lowered:
            return index
    return 0


def _protocol_label(protocol: str) -> str:
    try:
        from .models import PROTOCOL_SHORT_LABELS
    except ImportError:  # pragma: no cover - 兼容直接 import 的测试场景
        try:
            from models import PROTOCOL_SHORT_LABELS  # type: ignore
        except ImportError:
            PROTOCOL_SHORT_LABELS = {
                "openai": "OpenAI 兼容",
                "gemini": "Gemini 兼容",
                "grok": "Grok 兼容",
            }
    return PROTOCOL_SHORT_LABELS.get(protocol, protocol or "")


__all__ = ["OVERRIDE_KEYS", "STORE_KEY", "SessionState"]