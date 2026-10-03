"""插件配置访问层（冻结接口，除主代理外请勿修改）。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .models import (
    DEFAULT_PROTOCOL_MODELS,
    DEFAULT_SIZE,
    PROTOCOL_KEYS,
    PROTOCOL_LABELS,
    PROTOCOL_ORDER,
    PROTOCOL_SHORT_LABELS,
    Channel,
    default_edit_model_for,
    default_model_for,
    filter_sizes_for_protocol,
    looks_like_size,
    normalize_protocol,
    normalize_size,
    resolve_choice,
)

DEFAULT_CHANNELS: list[dict[str, Any]] = [
    {
        "__template_key": "openai",
        "name": "OpenAI 兼容",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-image-2",
        "edit_model": "",
    },
    {
        "__template_key": "gemini",
        "name": "Gemini 兼容",
        "base_url": "https://generativelanguage.googleapis.com",
        "api_key": "",
        "model": "gemini-2.5-flash-image",
        "edit_model": "",
    },
    {
        "__template_key": "grok",
        "name": "Grok 兼容",
        "base_url": "https://api.x.ai/v1",
        "api_key": "",
        "model": "grok-2-image",
        "edit_model": "",
    },
]

#: 默认供应商（v1.0.1 起域名 + 密钥只填一次，三种协议共用）
DEFAULT_SUPPLIERS: list[dict[str, Any]] = [
    {
        "name": "默认中转站",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
    },
]

#: 默认供应商名称
DEFAULT_SUPPLIER_NAME = DEFAULT_SUPPLIERS[0]["name"]

#: 默认菜单分组，保证新安装和旧配置升级后直接可用。
DEFAULT_MENU_SECTIONS: list[dict[str, Any]] = [
    {"name": "绘画与编辑", "items": ["绘画", "图片编辑", "重画", "抽卡", "预设列表", "图片转提示词"]},
    {"name": "模型与接口", "items": ["模型列表", "切换模型", "协议列表", "切换协议", "供应商列表", "切换供应商"]},
    {"name": "尺寸设置", "items": ["尺寸列表", "切换尺寸", "添加尺寸", "删除尺寸"]},
    {"name": "群与权限", "items": ["群列表", "群开关", "添加主人", "删除主人"]},
    {"name": "娱乐与统计", "items": ["排行", "运行统计", "我的ID"]},
    {"name": "插件维护", "items": ["菜单列表", "设置菜单", "删除菜单", "添加预设", "删除预设", "重载配置", "重置设置"]},
]

#: v1.0.3 及更早版本的内置分组名称，用于升级时清理旧出厂菜单。
_LEGACY_MENU_SECTION_NAMES = (
    "绘画与编辑",
    "模型与接口",
    "尺寸设置",
    "群与权限",
    "娱乐与统计",
    "插件维护",
)
_LEGACY_MENU_SECTION_ITEMS = (
    ("绘画", "图片编辑", "重画", "抽卡", "预设列表", "图片转提示词"),
    ("模型列表", "切换模型", "协议列表", "切换协议", "供应商列表", "切换供应商"),
    ("尺寸列表", "切换尺寸", "添加尺寸", "删除尺寸"),
    ("群列表", "群开关", "添加主人", "删除主人"),
    ("排行", "运行统计", "我的ID"),
    ("菜单列表", "设置菜单", "删除菜单", "添加预设", "删除预设", "重载配置", "重置设置"),
)

#: 菜单分组数量上限，避免配置无限膨胀
MENU_SECTION_LIMIT = 20

#: 单个分组的指令条数上限
MENU_SECTION_ITEM_LIMIT = 40

#: 供应商 / 协议 / 统计类指令默认名（顺序固定，恒为 8 项）
DEFAULT_SUPPLIER_COMMANDS: list[str] = [
    "供应商列表",
    "切换供应商",
    "协议列表",
    "切换协议",
    "模型列表",
    "切换模型",
    "运行统计",
    "重置设置",
]

#: 触发方式：at=需要 @机器人 / 唤醒前缀（默认）；command=消息以指令开头即响应
TRIGGER_MODES: tuple[str, ...] = ("at", "command")

#: 单协议最多缓存的远端模型条数
MODEL_CACHE_LIMIT = 200

#: 批量出图张数默认值与上限（需求 3）
BATCH_MAX_DEFAULT = 4
BATCH_MAX_LIMIT = 10

#: 预设（内置提示词）条数上限，避免配置无限膨胀（需求 14 / 17）
PROMPT_LIMIT = 200

#: 默认的提示词翻译 / 润色系统提示词（需求 7）
DEFAULT_TRANSLATE_PROMPT = (
    "你是资深 AI 绘画提示词工程师。请把用户的中文口语描述改写为适合文生图模型的结构化英文提示词：\n"
    "1. 忠实保留用户的主体、动作、风格意图，不要凭空添加人物或品牌；\n"
    "2. 按「主体 + 外观细节 + 场景 + 光线 + 镜头/构图 + 风格 + 画质」的顺序组织，逗号分隔；\n"
    "3. 第一行输出英文提示词，第二行以「中文摘要：」开头给出简短中文概括；\n"
    "4. 只输出提示词本身，不要解释、不要 Markdown 代码块、不要引号。"
)

DEFAULT_SIZES: list[str] = [
    "auto", "1024x1024", "1536x1024", "1024x1536", "2048x2048", "4096x4096",
    "1152x1536", "1536x1152", "720x1280", "1280x720", "2048x878", "878x2048",
]

DEFAULT_START_PROMPT = (
    "🎨 收到，正在为你{prompt_type}…\n"
    "🧠 模型：{模型}（{协议}）\n"
    "📐 尺寸：{尺寸}\n"
    "⏳ 最长等待 {超时} 秒，请稍候～"
)

DEFAULT_DONE_PROMPT = (
    "✅ {prompt_type}完成！\n"
    "🖼 出图 {数量} 张\n"
    "⏱ 耗时 {耗时} 秒\n"
    "🧠 {模型} · {尺寸}"
)

#: 默认菜单文案只展示运行状态和用户自定义分组，不内置功能指令清单。
DEFAULT_MENU_TEXT = (
    "gpt-image-2.5\n"
    "供应商：{供应商}\n"
    "协议：{协议}\n"
    "模型：{当前模型}\n"
    "尺寸：{当前尺寸}\n"
    "群状态：{群状态}\n"
    "{分组菜单}"
)

DEFAULT_DRAW_COMMANDS: list[str] = ["绘画", "画图"]
DEFAULT_EDIT_COMMANDS: list[str] = ["图片编辑", "编辑图片"]
DEFAULT_MENU_COMMANDS: list[str] = ["绘画菜单", "菜单"]
DEFAULT_MASTER_COMMANDS: list[str] = [
    "切换模型",
    "切换尺寸",
    "切换协议",
    "切换供应商",
    "添加尺寸",
    "删除尺寸",
    "群开关",
    "添加主人",
    "删除主人",
    "重载配置",
    "运行统计",
    "重置设置",
    "添加预设",
    "删除预设",
    "设置菜单",
    "删除菜单",
]

_RUNTIME_KEYS = {
    "session_overrides",
    "_session_overrides",
    "_state",
}

#: 机器人实例级覆盖支持的字段（需求 2）
_BOT_OVERRIDE_FIELDS = ("supplier", "protocol", "model", "size")

_NESTED_LAYOUT = {
    "base": {
        "active_provider": "OpenAI 兼容",
        "active_supplier": DEFAULT_SUPPLIER_NAME,
        "active_protocol": PROTOCOL_ORDER[0],
        "active_size": DEFAULT_SIZE,
        "timeout": 600,
        "trigger_mode": TRIGGER_MODES[0],
        "cooldown": 0,
        "retry_times": 1,
        "proxy": "",
        "private_enabled": True,
    },
    "commands": {
        "draw_commands": list(DEFAULT_DRAW_COMMANDS),
        "edit_commands": list(DEFAULT_EDIT_COMMANDS),
        "menu_commands": list(DEFAULT_MENU_COMMANDS),
        "master_commands": list(DEFAULT_MASTER_COMMANDS),
        "supplier_commands": list(DEFAULT_SUPPLIER_COMMANDS),
    },
    "messages": {
        "start_prompt": DEFAULT_START_PROMPT,
        "done_prompt": DEFAULT_DONE_PROMPT,
        "menu_text": DEFAULT_MENU_TEXT,
    },
    "suppliers": {
        "suppliers": [dict(item) for item in DEFAULT_SUPPLIERS],
    },
    "models": {
        "protocol_models": {
            protocol: dict(bucket)
            for protocol, bucket in DEFAULT_PROTOCOL_MODELS.items()
        },
        "model_cache": {},
    },
    "access": {
        "masters": [],
        "masters_use_astrbot_admin": True,
        "enabled_platforms": ["aiocqhttp", "qq_official", "qq_official_webhook"],
        "enabled_bot_ids": [],
        "group_mode": "all",
        "group_list": [],
        # 需求 16：群必须由主人显式开启后才执行指令（默认开启该约束）
        "group_require_enable": True,
        # 需求 12：群友黑名单（支持 "用户ID" 或 "平台实例ID|用户ID"）
        "user_blacklist": [],
    },
    # v1.0.2 新增：智能能力 / 娱乐玩法 / 批量出图等配置（需求 3、7~11、13~17）
    "extras": {
        "batch_max": BATCH_MAX_DEFAULT,
        "size_auto_detect": True,
        "strict_trigger": True,
        # prompts 用 None 表示「从未自定义过」，此时回落到 core.presets 内置库
        "prompts": None,
        "gacha_enabled": True,
        "gacha_styles": [],
        "rank_enabled": True,
        "flavor_enabled": True,
        "flavor_lines": [],
        "img2prompt_enabled": False,
        "img2prompt_model": "",
        "translate_enabled": False,
        "translate_model": "",
        "translate_supplier": "",
        "translate_prompt_text": "",
        # 需求 2：机器人实例级覆盖 {平台实例ID: {supplier/protocol/model/size}}
        "bot_overrides": {},
        "quality": "auto",
        "transparent_background": False,
        "qq_file_fallback": True,
        "auto_reload": True,
    },
    "limits": {
        "max_input_images": 6,
        "max_concurrent": 2,
        "reply_reference_image": True,
    },
    # 需求 2：自定义菜单分组（用户可用指令增删改）
    "menus": {"menu_sections": None},
    "sizes": {"sizes": list(DEFAULT_SIZES)},
}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except Exception:  # noqa: BLE001
                parsed = None
            if isinstance(parsed, list):
                return list(parsed)
        return [item.strip() for item in text.replace("，", ",").split(",") if item.strip()]
    return [value]


def _as_str_list(value: Any) -> list[str]:
    result: list[str] = []
    for item in _as_list(value):
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


#: 字符串真值 / 假值集合（供 _as_bool 与 _as_bool_strict 共用）
_TRUE_TOKENS = frozenset({"1", "true", "yes", "y", "on", "是", "开启", "启用", "开"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "n", "off", "否", "关闭", "禁用", "关"})


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_TOKENS
    if value is None:
        return default
    return bool(value)


def _as_bool_strict(value: Any, default: bool) -> bool:
    """比 :func:`_as_bool` 更保守的布尔解析：无法识别时返回 ``default``。

    用于 v1.0.2 新增的安全相关开关（例如「群必须由主人开启」），避免用户误填
    乱码后被解析成 ``False`` 而导致放开限制。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_TOKENS:
            return True
        if text in _FALSE_TOKENS:
            return False
        return default
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_id_list(value: Any) -> list[str]:
    """解析「用户 ID / 群 ID」类列表：只接受列表与字符串，其它类型一律忽略。"""
    if isinstance(value, (list, tuple)):
        return _as_str_list(value)
    if isinstance(value, str):
        return _as_str_list(value)
    return []


def _as_text(value: Any, default: str = "") -> str:
    """只接受字符串；其它类型一律返回 ``default``（避免把数字 / 字典当文案用）。"""
    if isinstance(value, str):
        text = value.strip()
        return text if text else default
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:  # NaN
        return default
    return result


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_plain_dict(value: Any) -> dict[str, list[str]]:
    """把 {协议: [模型...]} 形式的缓存归一化成去重去空的字典。"""
    result: dict[str, list[str]] = {}
    for key, items in _as_dict(value).items():
        protocol = normalize_protocol(str(key)) or str(key or "").strip().lower()
        if not protocol:
            continue
        cleaned = [item for item in _as_str_list(items) if item]
        if cleaned:
            result[protocol] = cleaned[:MODEL_CACHE_LIMIT]
    return result


def _normalize_prompt_entry(entry: Any) -> dict[str, Any] | None:
    """把内置提示词条目归一化成 {name, text, tags}；非法输入返回 None。"""
    if isinstance(entry, dict):
        name = str(entry.get("name") or "").strip()
        text = str(entry.get("text") or "").strip()
        tags = _as_str_list(entry.get("tags"))
    elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
        name = str(entry[0] or "").strip()
        text = str(entry[1] or "").strip()
        tags = _as_str_list(entry[2]) if len(entry) > 2 else []
    else:
        return None
    if not name or not text:
        return None
    return {"name": name, "text": text, "tags": tags}


def _normalize_prompt_list(value: Any) -> list[dict[str, Any]]:
    """归一化提示词列表：按名称去重、去空、限制条数。"""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _as_list(value):
        entry = _normalize_prompt_entry(item)
        if entry is None or entry["name"] in seen:
            continue
        seen.add(entry["name"])
        result.append(entry)
        if len(result) >= PROMPT_LIMIT:
            break
    return result


def _normalize_bot_overrides(value: Any) -> dict[str, dict[str, str]]:
    """归一化机器人实例覆盖表，丢弃空实例与空字段。"""
    result: dict[str, dict[str, str]] = {}
    for key, bucket in _as_dict(value).items():
        platform_id = str(key or "").strip()
        if not platform_id or not isinstance(bucket, dict):
            continue
        cleaned: dict[str, str] = {}
        for field in _BOT_OVERRIDE_FIELDS:
            text = str(bucket.get(field) or "").strip()
            if text:
                cleaned[field] = text
        if cleaned:
            result[platform_id] = cleaned
    return result


def _normalize_style_entries(value: Any) -> list[dict[str, Any]]:
    """归一化抽卡画风池：{name, text, rarity}，rarity 仅保留 common/rare/epic。"""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _as_list(value):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        text = str(item.get("text") or "").strip()
        rarity = str(item.get("rarity") or "common").strip().lower()
        if rarity not in {"common", "rare", "epic"}:
            rarity = "common"
        if not name or not text or name in seen:
            continue
        seen.add(name)
        result.append({"name": name, "text": text, "rarity": rarity})
    return result


def _normalize_menu_sections(value: Any) -> list[dict[str, Any]]:
    """归一化自定义菜单分组：{name, items}，去重去空并限制规模。"""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _as_list(value):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        items = _as_str_list(item.get("items"))
        if not name or name in seen:
            continue
        seen.add(name)
        result.append({"name": name, "items": items[:MENU_SECTION_ITEM_LIMIT]})
        if len(result) >= MENU_SECTION_LIMIT:
            break
    return result


def _normalize_text_list(value: Any) -> list[str]:
    """归一化文案池：去重、去空。"""
    return _as_str_list(value)


def _mask_api_key(api_key: str) -> str:
    """生成密钥掩码；沿用 Channel.masked_key 的规则，保持前后端一致。"""
    text = str(api_key or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}****{text[-4:]}"


def _normalize_supplier_entry(entry: Any) -> dict[str, Any] | None:
    """把配置项归一化成 {name, base_url, api_key}；非法输入返回 None。"""
    if not isinstance(entry, dict):
        return None
    entry = _supplier_payload(entry)
    name = str(entry.get("name") or "").strip()
    if not name:
        return None
    return {
        "name": name,
        "base_url": str(entry.get("base_url") or "").strip(),
        "api_key": str(entry.get("api_key") or "").strip(),
    }


def _supplier_payload(entry: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key in ("config", "data"):
        if isinstance(entry.get(key), dict):
            result.update(entry[key])
    result.update({key: value for key, value in entry.items() if key not in ("config", "data")})
    return result


def _supplier_sources(raw: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    nested = _as_dict(_as_dict(raw.get("nested")).get("suppliers"))
    sources = {}
    for key in ("suppliers", "providers", "channels"):
        for prefix, container in (("", raw), ("nested.", nested)):
            value = container.get(key)
            if not isinstance(value, list):
                continue
            sources[prefix + key] = [
                _supplier_payload(entry) for entry in value
                if _normalize_supplier_entry(entry) is not None
            ]
    return sources


def _supplier_digest(entries: list[dict[str, Any]]) -> str:
    value = [_normalize_supplier_entry(entry) for entry in entries]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _supplier_source(raw: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    sources = _supplier_sources(raw)
    sync = _as_dict(raw.get("_suppliers_sync"))
    canonical = next((key for key in ("suppliers", "nested.suppliers") if sources.get(key)), "")
    if canonical and sync.get(canonical) != _supplier_digest(sources[canonical]):
        return canonical, sources[canonical]
    for key, entries in sources.items():
        if key in sync and sync[key] != _supplier_digest(entries):
            return key, entries
    return next(((key, entries) for key, entries in sources.items() if entries), ("", []))


def _supplier_config_entry(entry: dict[str, Any], template_key: str) -> dict[str, Any]:
    result = {
        "__template_key": template_key,
        "name": entry["name"],
        "base_url": entry["base_url"],
        "api_key": entry["api_key"],
    }
    if template_key != "supplier":
        result.update({"model": "", "edit_model": ""})
    return result


def _normalize_protocol_models(value: Any) -> dict[str, dict[str, str]]:
    """把 protocol_models 归一化成 {协议: {model, edit_model}}，缺项用出厂值补齐。"""
    raw = _as_dict(value)
    result: dict[str, dict[str, str]] = {}
    for protocol in PROTOCOL_ORDER:
        entry = raw.get(protocol)
        model = ""
        edit_model = ""
        if isinstance(entry, dict):
            model = str(entry.get("model") or "").strip()
            edit_model = str(entry.get("edit_model") or "").strip()
        elif isinstance(entry, str):
            model = entry.strip()
        result[protocol] = {
            "model": model or default_model_for(protocol),
            "edit_model": edit_model,
        }
    return result


class PluginConfig:
    """插件配置的读写门面，兼容 AstrBotConfig 与普通 dict。"""

    def __init__(self, raw: Any = None) -> None:
        self._raw: Any = raw if raw is not None else {}
        self._cache: dict[str, Any] = {}
        self._model_cache_catalogs: dict[tuple[str, ...], list[str]] = {}
        self.reload()

    # ------------------------------------------------------------------ 基础
    def bind(self, raw: Any) -> None:
        self._raw = raw if raw is not None else {}
        self.reload()

    def raw(self) -> Any:
        return self._raw

    def raw_dict(self) -> dict[str, Any]:
        try:
            data = dict(self._raw)
        except Exception:  # noqa: BLE001
            return {}
        data.pop("_cache", None)
        for key in list(data):
            if key in _RUNTIME_KEYS:
                data.pop(key)
        return data

    def reload(self) -> None:
        raw = self._raw if isinstance(self._raw, dict) else {}
        cache: dict[str, Any] = {}
        self._model_cache_catalogs = {}

        # 旧通道：优先读取根级 providers / channels（仅用于兼容读取与迁移）
        sources = _supplier_sources(raw)
        channels_raw = next((entries for key, entries in sources.items() if key.endswith(("providers", "channels")) and entries), [])
        if isinstance(channels_raw, list) and channels_raw:
            cache["channels"] = list(channels_raw)
        else:
            cache["channels"] = [dict(item) for item in DEFAULT_CHANNELS]

        # 供应商：优先读取根级 suppliers；框架注入的默认 suppliers 不应遮蔽旧 providers
        nested = raw.get("nested") if isinstance(raw.get("nested"), dict) else {}
        _, suppliers_raw = _supplier_source(raw)
        normalized_suppliers: list[dict[str, Any]] = []
        if isinstance(suppliers_raw, list):
            for entry in suppliers_raw:
                supplier = _normalize_supplier_entry(entry)
                if supplier is not None:
                    normalized_suppliers.append(supplier)
        cache["suppliers"] = normalized_suppliers or [
            dict(item) for item in DEFAULT_SUPPLIERS
        ]

        # 尺寸列表：兼容 sizes（内部键）与 image_sizes（经典配置页可见键）。
        # 两个键都可能被用户改动（经典页只显示 image_sizes），所以用上次同步值
        # _sizes_sync 做「谁被改过」的判定，避免经典页改了却读不到（v1.0.1 的 bug）。
        sizes_sync = _as_str_list(raw.get("_sizes_sync"))
        sizes_internal = _as_str_list(raw.get("sizes"))
        sizes_classic = _as_str_list(raw.get("image_sizes"))
        if sizes_sync:
            internal_changed = sizes_internal != sizes_sync
            classic_changed = sizes_classic != sizes_sync
            if internal_changed and not classic_changed:
                sizes_raw = sizes_internal
            elif classic_changed and not internal_changed:
                sizes_raw = sizes_classic
            elif internal_changed and classic_changed:
                # 两边都被改过：经典页是唯一可见入口，优先采用它
                sizes_raw = sizes_classic or sizes_internal
            else:
                sizes_raw = sizes_internal or sizes_classic
        else:
            sizes_raw = sizes_internal or sizes_classic

        # 组布局：兼容扁平与嵌套（nested.*）
        for section, defaults in _NESTED_LAYOUT.items():
            section_data = nested.get(section) if isinstance(nested.get(section), dict) else {}
            for key, default in defaults.items():
                if key == "sizes":
                    # 尺寸列表已在上面单独解析（兼容双键名）
                    cache[key] = _as_str_list(sizes_raw) or list(default)
                    continue
                if key == "suppliers":
                    # 供应商已在上面统一归一化（兼容旧 providers 与默认值）
                    continue
                if key == "protocol_models":
                    value = raw.get(key)
                    if value is None:
                        value = section_data.get(key)
                    cache[key] = _normalize_protocol_models(value)
                    continue
                if key == "model_cache":
                    value = raw.get(key)
                    if value is None:
                        value = section_data.get(key)
                    cache[key] = _as_plain_dict(value)
                    continue
                if key == "bot_overrides":
                    value = raw.get(key)
                    if value is None:
                        value = section_data.get(key)
                    cache[key] = _normalize_bot_overrides(value)
                    continue
                if key == "user_blacklist":
                    value = raw.get(key)
                    if value is None:
                        value = section_data.get(key)
                    cache[key] = _as_id_list(value)
                    continue
                if key == "menu_sections":
                    # None = 从未自定义过（回落到出厂分组）；[] 表示用户已清空
                    if raw.get(key) is not None:
                        cache[key] = _normalize_menu_sections(raw.get(key))
                    elif section_data.get(key) is not None:
                        cache[key] = _normalize_menu_sections(section_data.get(key))
                    else:
                        cache[key] = None
                    continue
                if key == "prompts":
                    # None = 从未自定义过（回落到内置库）；[] 表示用户已清空
                    if raw.get(key) is not None:
                        cache[key] = _normalize_prompt_list(raw.get(key))
                    elif section_data.get(key) is not None:
                        cache[key] = _normalize_prompt_list(section_data.get(key))
                    else:
                        cache[key] = default
                    continue
                if key in raw and raw.get(key) is not None:
                    cache[key] = raw.get(key)
                elif key in section_data and section_data.get(key) is not None:
                    cache[key] = section_data.get(key)
                else:
                    cache[key] = default

        # 老配置迁移：v1.0.1 没有 group_require_enable 键（默认即 True），
        # 而 all / blacklist 语义与「群必须由主人开启」相反，需收敛为空白名单。
        # 用户显式写过该键时不干预（尊重其选择）。
        require_raw = raw.get("group_require_enable")
        if require_raw is None:
            access_section = (
                nested.get("access") if isinstance(nested.get("access"), dict) else {}
            )
            require_raw = access_section.get("group_require_enable")
        if require_raw is None and _as_bool(cache.get("group_require_enable"), True):
            if cache.get("group_mode") == "blacklist":
                # 黑名单里是「已关闭的群」，在新语义下会被误判为「已开启」，
                # 直接清空（fail-closed），由主人重新逐个开启。
                cache["group_list"] = []
            if cache.get("group_mode") != "whitelist":
                cache["group_mode"] = "whitelist"

        # 会话运行态等运行时键
        for key in _RUNTIME_KEYS:
            if key in raw:
                cache[key] = raw.get(key)

        self._cache = cache

    # ------------------------------------------------------------------ 读写
    def get(self, key: str, default: Any = None) -> Any:
        if key in self._cache:
            return self._cache[key]
        if isinstance(self._raw, dict) and key in self._raw:
            return self._raw.get(key)
        return default

    def set(self, key: str, value: Any) -> None:
        self._cache[key] = value

    def set_raw(self, key: str, value: Any) -> None:
        self._cache[key] = value
        if isinstance(self._raw, dict):
            self._raw[key] = value
            # 尺寸三键（sizes / image_sizes / _sizes_sync）始终保持一致：
            # 无论哪条路径写入（设置页、经典配置页、聊天指令），都同步另外两个，
            # 避免出现「设置页改了、插件读到的还是旧值」这类不一致。
            if key in ("sizes", "image_sizes"):
                normalized = _as_str_list(value)
                self._raw["sizes"] = list(normalized)
                self._raw["image_sizes"] = list(normalized)
                self._raw["_sizes_sync"] = list(normalized)
                self._cache["sizes"] = list(normalized)
                self._cache["image_sizes"] = list(normalized)
                self._cache["_sizes_sync"] = list(normalized)

    #: 不写入配置文件的内部键（缓存 / 派生视图）
    _INTERNAL_KEYS = ("channels", "suppliers")

    def _sync_to_raw(self) -> None:
        """把缓存中的配置项写回底层 dict（跳过内部键与运行时键）。"""
        raw = self._raw
        if not isinstance(raw, dict):
            return
        for key, value in self._cache.items():
            if key in self._INTERNAL_KEYS or key in _RUNTIME_KEYS:
                continue
            if value is None:
                # None 表示「从未设置」（如 prompts 未自定义），不落盘，
                # 避免写入 null 触发框架配置类型校验
                continue
            raw[key] = value

    def save(self) -> bool:
        """持久化配置；AstrBotConfig 有 save_config 时直接调用。"""
        if not isinstance(self._raw, dict):
            return False
        self._write_suppliers(self._supplier_entries())
        self._sync_to_raw()
        saver = getattr(self._raw, "save_config", None)
        if callable(saver):
            try:
                saver()
                return True
            except TypeError:
                try:
                    saver(dict(self._raw))
                    return True
                except Exception:  # noqa: BLE001
                    return False
            except Exception:  # noqa: BLE001
                return False
        return False

    # ------------------------------------------------------------------ 供应商
    def _supplier_entries(self) -> list[dict[str, Any]]:
        """返回归一化后的供应商条目；始终至少有一个（缺省时为默认中转站）。"""
        entries: list[dict[str, Any]] = []
        for entry in _as_list(self._cache.get("suppliers")):
            supplier = _normalize_supplier_entry(entry)
            if supplier is not None and supplier["name"] not in [item["name"] for item in entries]:
                entries.append(supplier)
        if not entries:
            entries = [dict(item) for item in DEFAULT_SUPPLIERS]
        return entries

    def _write_suppliers(self, entries: list[dict[str, Any]]) -> None:
        """写回 suppliers（新键）与 providers（旧键兼容副本）。"""
        normalized: list[dict[str, Any]] = []
        for entry in entries:
            supplier = _normalize_supplier_entry(entry)
            if supplier is not None:
                normalized.append(supplier)
        if not normalized:
            normalized = [dict(item) for item in DEFAULT_SUPPLIERS]
        canonical = [
            _supplier_config_entry(item, "supplier") for item in normalized
        ]
        self.set_raw("suppliers", canonical)
        self._cache["suppliers"] = canonical
        if isinstance(self._raw, dict):
            legacy = [_supplier_config_entry(item, "openai") for item in normalized]
            self._raw["providers"] = legacy
            self._cache["channels"] = legacy
            if "channels" in self._raw:
                self._raw["channels"] = [dict(item) for item in legacy]
            nested = _as_dict(_as_dict(self._raw.get("nested")).get("suppliers"))
            for key in ("suppliers", "providers", "channels"):
                if key in nested:
                    nested[key] = [dict(item) for item in (canonical if key == "suppliers" else legacy)]
            if nested:
                self._raw["nested"]["suppliers"] = nested
            self.set_raw("_suppliers_sync", {
                key: _supplier_digest(value)
                for key, value in _supplier_sources(self._raw).items()
            })

    def replace_suppliers(self, suppliers: Any) -> bool:
        """整体替换供应商列表（设置页保存用）：先清空再写入，避免残留旧条目。

        ``suppliers`` 为空或全部非法时不做修改，返回 False。
        """
        entries: list[dict[str, Any]] = []
        for item in _as_list(suppliers):
            if isinstance(item, Channel):
                entry = {
                    "name": str(item.name or "").strip(),
                    "base_url": str(item.base_url or "").strip(),
                    "api_key": str(item.api_key or "").strip(),
                }
            else:
                entry = _normalize_supplier_entry(item)
            if entry is None:
                continue
            if entry["name"] in [existing["name"] for existing in entries]:
                continue
            entries.append(entry)
        if not entries:
            return False
        self._write_suppliers(entries)
        names = [item["name"] for item in entries]
        if self.active_supplier_name() not in names:
            self.set_raw("active_supplier", names[0])
        return True

    def suppliers(self) -> list[Channel]:
        """供应商列表；Channel 的 protocol 字段为占位，有效字段为 name/base_url/api_key。"""
        result: list[Channel] = []
        for entry in self._supplier_entries():
            result.append(
                Channel(
                    protocol=PROTOCOL_ORDER[0],
                    name=entry["name"],
                    base_url=entry["base_url"],
                    api_key=entry["api_key"],
                )
            )
        return result

    def supplier_names(self) -> list[str]:
        return [supplier.name for supplier in self.suppliers()]

    def get_supplier(self, name: str) -> Channel | None:
        """按名称 / 名称忽略大小写 / 1-based 序号字符串查找供应商。"""
        target = str(name or "").strip()
        if not target:
            return None
        suppliers = self.suppliers()
        index, _value = resolve_choice(target, [item.name for item in suppliers])
        if index < 0:
            return None
        return suppliers[index]

    def active_supplier_name(self) -> str:
        name = str(self._cache.get("active_supplier") or "").strip()
        if name:
            supplier = self.get_supplier(name)
            if supplier is not None:
                return supplier.name
        # 旧键兼容：active_provider 直接当供应商名用
        legacy = str(self._cache.get("active_provider") or "").strip()
        if legacy:
            supplier = self.get_supplier(legacy)
            if supplier is not None:
                return supplier.name
        names = self.supplier_names()
        return names[0] if names else ""

    def active_supplier(self) -> Channel | None:
        return self.get_supplier(self.active_supplier_name())

    def set_active_supplier(self, value: str) -> bool:
        supplier = self.get_supplier(value)
        if supplier is None:
            return False
        self.set_raw("active_supplier", supplier.name)
        if isinstance(self._raw, dict):
            self._raw.pop("active_provider", None)
        self._cache.pop("active_provider", None)
        return True

    def upsert_supplier(self, supplier: Channel) -> None:
        pass  # 占位
        name = str(getattr(supplier, "name", "") or "").strip()
        if not name:
            return
        entries = self._supplier_entries()
        updated = False
        for index, entry in enumerate(entries):
            if entry["name"] == name:
                entries[index] = {
                    "name": name,
                    "base_url": str(getattr(supplier, "base_url", "") or "").strip(),
                    "api_key": str(getattr(supplier, "api_key", "") or "").strip(),
                }
                updated = True
                break
        if not updated:
            entries.append(
                {
                    "name": name,
                    "base_url": str(getattr(supplier, "base_url", "") or "").strip(),
                    "api_key": str(getattr(supplier, "api_key", "") or "").strip(),
                }
            )
        self._write_suppliers(entries)

    def remove_supplier(self, name: str, keep_last: bool = True) -> bool:
        """删除供应商；``keep_last=False`` 时允许删空（供设置页整体覆盖使用）。"""
        supplier = self.get_supplier(name)
        if supplier is None:
            return False
        entries = self._supplier_entries()
        remaining = [item for item in entries if item["name"] != supplier.name]
        if len(remaining) == len(entries):
            return False
        if not remaining and keep_last:
            # 始终保留至少一个供应商，避免配置为空后无处可发请求
            return False
        self._write_suppliers(remaining)
        if self.active_supplier_name() not in [item["name"] for item in remaining]:
            self.set_raw("active_supplier", remaining[0]["name"] if remaining else "")
        return True

    # ------------------------------------------------------- 通道（旧接口兼容）
    def channels(self) -> list[Channel]:
        """旧接口兼容：等价于 suppliers()。"""
        return self.suppliers()

    def channel_names(self) -> list[str]:
        """旧接口兼容：等价于 supplier_names()。"""
        return self.supplier_names()

    def get_channel(self, name: str) -> Channel | None:
        """旧接口兼容：按供应商名查找。"""
        return self.get_supplier(name)

    def active_channel_name(self) -> str:
        """旧接口兼容：当前供应商名。"""
        return self.active_supplier_name()

    def active_channel(self) -> Channel | None:
        """旧接口兼容：等价于 active_channel()/build_channel()。"""
        return self.build_channel()

    def set_active_channel(self, name: str) -> bool:
        """旧接口兼容：等价于 set_active_supplier()。"""
        return self.set_active_supplier(name)

    def upsert_channel(self, channel: Channel) -> None:
        """旧接口兼容：写供应商（协议 / 模型字段不再随通道保存）。"""
        self.upsert_supplier(channel)

    def remove_channel(self, name: str) -> bool:
        """旧接口兼容：删除供应商。"""
        return self.remove_supplier(name)

    # ------------------------------------------------------------------ 协议
    def protocols(self) -> list[str]:
        """协议列表，恒为 ["openai", "gemini", "grok"]。"""
        return list(PROTOCOL_ORDER)

    def active_protocol(self) -> str:
        value = str(self._cache.get("active_protocol") or "").strip()
        return normalize_protocol(value) or PROTOCOL_ORDER[0]

    def set_active_protocol(self, value: str) -> bool:
        """支持协议 key / 中文名 / 1-based 序号。"""
        protocol = normalize_protocol(value)
        if not protocol:
            return False
        self.set_raw("active_protocol", protocol)
        return True

    # -------------------------------------------------------------- 模型（按协议）
    def _protocol_models_cache(self) -> dict[str, dict[str, str]]:
        return _normalize_protocol_models(self._cache.get("protocol_models"))

    def _write_protocol_models(self, models: dict[str, dict[str, str]]) -> None:
        normalized = _normalize_protocol_models(models)
        self.set_raw("protocol_models", normalized)
        self._cache["protocol_models"] = normalized

    def model_for(self, protocol: str = "") -> str:
        key = normalize_protocol(protocol) or self.active_protocol()
        return self._protocol_models_cache()[key]["model"] or default_model_for(key)

    def edit_model_for(self, protocol: str = "") -> str:
        key = normalize_protocol(protocol) or self.active_protocol()
        bucket = self._protocol_models_cache()[key]
        return bucket["edit_model"] or bucket["model"] or default_model_for(key)

    def set_model_for(self, protocol: str, model: str) -> None:
        key = normalize_protocol(protocol)
        if not key:
            return
        models = self._protocol_models_cache()
        models[key]["model"] = str(model or "").strip()
        self._write_protocol_models(models)

    def set_edit_model_for(self, protocol: str, model: str) -> None:
        key = normalize_protocol(protocol)
        if not key:
            return
        models = self._protocol_models_cache()
        models[key]["edit_model"] = str(model or "").strip()
        self._write_protocol_models(models)

    def protocol_models(self) -> dict[str, dict[str, str]]:
        """归一化后的 {协议: {model, edit_model}}。"""
        return self._protocol_models_cache()

    def models_for(self, protocol: str = "") -> list[str]:
        """当前协议已知的模型候选列表（有序、去重、去空）。

        顺序固定为：生成模型、编辑模型、本地缓存的远端模型列表。任何异常都返回 []。
        """
        try:
            key = normalize_protocol(protocol) or self.active_protocol()
            candidates: list[str] = [self.model_for(key), self.edit_model_for(key)]
            candidates.extend(self.model_cache(key))
            result: list[str] = []
            for item in candidates:
                text = str(item or "").strip()
                if text and text not in result:
                    result.append(text)
            return result
        except Exception:  # noqa: BLE001 - 模型列表读取失败不应影响调用方
            return []

    def model_cache(self, protocol: str = "") -> list[str]:
        """返回某个协议本地缓存的远端模型列表（协议留空取当前协议）。"""
        key = normalize_protocol(protocol) or self.active_protocol()
        cached = _as_plain_dict(self._cache.get("model_cache"))
        return list(cached.get(key, []))

    def _supplier_model_cache_key(
        self, protocol: str, supplier: str = ""
    ) -> tuple[str, ...] | None:
        key = normalize_protocol(protocol) or self.active_protocol()
        target = self.get_supplier(supplier) if str(supplier or "").strip() else self.active_supplier()
        if target is None:
            return None
        return (
            key,
            target.name,
            target.base_url,
            hashlib.sha256(target.api_key.encode("utf-8")).hexdigest(),
        )

    def model_cache_for_supplier(self, protocol: str = "", supplier: str = "") -> list[str]:
        """返回当前供应商与协议对应的运行时模型目录。

        旧版持久化缓存只有协议维度，无法确认来源供应商；未建立来源索引时宁可
        返回空列表，让调用方重新拉取，也不把其他供应商的模型展示出来。
        """
        cache_key = self._supplier_model_cache_key(protocol, supplier)
        if cache_key is None:
            return []
        return list(self._model_cache_catalogs.get(cache_key, []))

    def set_model_cache(self, protocol: str, models: Any) -> None:
        """写入某个协议的远端模型缓存（去重去空，最多 200 条）。"""
        entries = [item for item in _as_str_list(models) if item]
        key = normalize_protocol(protocol)
        if not key:
            return
        cached = _as_plain_dict(self._cache.get("model_cache"))
        if entries:
            cached[key] = entries[:MODEL_CACHE_LIMIT]
        else:
            cached.pop(key, None)
        self.set_raw("model_cache", cached)
        self._cache["model_cache"] = cached

    def set_model_cache_for_supplier(
        self, protocol: str, supplier: str, models: Any
    ) -> None:
        """写入当前供应商与协议对应的模型目录，并同步旧格式缓存。"""
        key = normalize_protocol(protocol)
        cache_key = self._supplier_model_cache_key(key, supplier)
        if not key or cache_key is None:
            return
        entries = [item for item in _as_str_list(models) if item]
        entries = entries[:MODEL_CACHE_LIMIT]
        if entries:
            self._model_cache_catalogs[cache_key] = list(entries)
        else:
            self._model_cache_catalogs.pop(cache_key, None)
        self.set_model_cache(key, entries)

    # ------------------------------------------------------------------ 统一出口
    def build_channel(
        self,
        *,
        supplier: str = "",
        protocol: str = "",
        model: str = "",
        edit_model: str = "",
        platform_id: str = "",
    ) -> Channel | None:
        """按供应商 + 协议 + 模型组合出可直接请求的 Channel；供应商不存在返回 None。

        参数留空时使用当前值；传入 ``platform_id`` 时会先套用该机器人实例
        的覆盖配置（需求 2：多机器人独立配置），未覆盖的字段仍取全局值。
        """
        if str(platform_id or "").strip():
            override = self.bot_override(platform_id)
            if override:
                supplier = supplier or override.get("supplier", "")
                protocol = protocol or override.get("protocol", "")
                model = model or override.get("model", "")
        target = self.get_supplier(supplier) if str(supplier or "").strip() else self.active_supplier()
        if target is None:
            return None
        key = normalize_protocol(protocol) or self.active_protocol()
        generate = str(model or "").strip() or self.model_for(key)
        edit = str(edit_model or "").strip() or self.edit_model_for(key)
        return Channel(
            protocol=key,
            name=target.name,
            base_url=target.base_url,
            api_key=target.api_key,
            model=generate,
            edit_model=edit,
        )

    def channel_for(self, platform_id: str) -> Channel | None:
        """按机器人实例 ID 组装通道（含实例级覆盖），供 main 直接调用。"""
        return self.build_channel(platform_id=platform_id)

    def size_for(self, platform_id: str) -> str:
        """某机器人实例生效的尺寸：实例覆盖优先，其次全局；自动剔除不支持的档位。"""
        size = str(self.bot_override(platform_id).get("size") or "").strip()
        protocol = str(self.bot_override(platform_id).get("protocol") or "").strip()
        key = normalize_protocol(protocol) or self.active_protocol()
        if size:
            normalized = normalize_size(size, size)
            if normalized in self.size_list_for(key):
                return normalized
        return self.active_size()

    def protocol_for(self, platform_id: str) -> str:
        """某机器人实例生效的协议（实例覆盖优先）。"""
        value = str(self.bot_override(platform_id).get("protocol") or "").strip()
        return normalize_protocol(value) or self.active_protocol()

    def model_for_bot(self, platform_id: str, protocol: str = "") -> str:
        """某机器人实例生效的生成模型：实例覆盖 > 指定协议 > 当前协议。"""
        override = self.bot_override(platform_id).get("model", "")
        key = normalize_protocol(protocol) or self.protocol_for(platform_id)
        return str(override or "").strip() or self.model_for(key)

    # ------------------------------------------------------- 触发 / 稳定性
    def trigger_mode(self) -> str:
        mode = str(self._cache.get("trigger_mode") or "").strip().lower()
        return mode if mode in TRIGGER_MODES else TRIGGER_MODES[0]

    def set_trigger_mode(self, mode: str) -> bool:
        target = str(mode or "").strip().lower()
        if target not in TRIGGER_MODES:
            return False
        self.set_raw("trigger_mode", target)
        return True

    def cooldown(self) -> float:
        value = _as_float(self._cache.get("cooldown"), 0.0)
        return value if value > 0 else 0.0

    def set_cooldown(self, seconds: Any) -> None:
        value = _as_float(seconds, 0.0)
        self.set_raw("cooldown", int(value) if float(value).is_integer() else value)

    def retry_times(self) -> int:
        return max(0, min(_as_int(self._cache.get("retry_times"), 1), 10))

    def set_retry_times(self, times: Any) -> None:
        self.set_raw("retry_times", max(0, min(_as_int(times, 1), 10)))

    def proxy(self) -> str:
        return str(self._cache.get("proxy") or "").strip()

    def set_proxy(self, value: str) -> None:
        self.set_raw("proxy", str(value or "").strip())

    def supplier_commands(self) -> list[str]:
        """供应商 / 协议 / 统计类指令名；恒返回 8 项，缺项用默认补齐。"""
        commands = _as_str_list(self._cache.get("supplier_commands"))
        result = commands[: len(DEFAULT_SUPPLIER_COMMANDS)]
        for index in range(len(result), len(DEFAULT_SUPPLIER_COMMANDS)):
            result.append(DEFAULT_SUPPLIER_COMMANDS[index])
        return result

    # ------------------------------------------------------------------ 迁移
    def migrate(self) -> bool:
        """把 v1.0.0 的旧配置迁移到 v1.0.1（供应商 + 协议模型）。有改动返回 True。"""
        if not isinstance(self._raw, dict):
            return False
        raw = self._raw
        changed = False

        source, legacy_entries = _supplier_source(raw)
        has_suppliers = source in ("suppliers", "nested.suppliers") or bool(raw.get("_suppliers_sync"))

        old_entries: list[Any] = []
        if not has_suppliers:
            if isinstance(legacy_entries, list):
                old_entries = list(legacy_entries)

        merged: list[dict[str, Any]] = []
        indexes: dict[str, int] = {}
        legacy_models: dict[str, dict[str, str]] = {}
        legacy_protocols: dict[str, str] = {}
        if old_entries:
            for entry in old_entries:
                channel = Channel.from_config_entry(entry)
                if channel is None or not channel.name:
                    continue
                if channel.protocol:
                    legacy_protocols.setdefault(channel.name, channel.protocol)
                if channel.name in indexes:
                    # 同名供应商合并：保留第一个非空密钥
                    index = indexes[channel.name]
                    if not merged[index]["api_key"] and channel.api_key:
                        merged[index]["api_key"] = channel.api_key
                    if not merged[index]["base_url"] and channel.base_url:
                        merged[index]["base_url"] = channel.base_url
                else:
                    indexes[channel.name] = len(merged)
                    merged.append(
                        {
                            "name": channel.name,
                            "base_url": channel.base_url,
                            "api_key": channel.api_key,
                        }
                    )
                if channel.model or channel.edit_model:
                    bucket = legacy_models.setdefault(
                        channel.protocol, {"model": "", "edit_model": ""}
                    )
                    if channel.model and not bucket["model"]:
                        bucket["model"] = channel.model
                    if channel.edit_model and not bucket["edit_model"]:
                        bucket["edit_model"] = channel.edit_model

        if merged:
            self._write_suppliers(merged)
            changed = True
        elif source and (
            raw.get("suppliers") != [_supplier_config_entry(item, "supplier") for item in self._supplier_entries()]
            or raw.get("_suppliers_sync") != {
                key: _supplier_digest(value) for key, value in _supplier_sources(raw).items()
            }
        ):
            self._write_suppliers(self._supplier_entries())
            changed = True

        if legacy_models:
            current = self._protocol_models_cache()
            for protocol, bucket in legacy_models.items():
                if bucket["model"]:
                    current[protocol]["model"] = bucket["model"]
                if bucket["edit_model"]:
                    current[protocol]["edit_model"] = bucket["edit_model"]
            normalized = _normalize_protocol_models(current)
            raw["protocol_models"] = normalized
            self._cache["protocol_models"] = normalized
            changed = True

        # active_provider -> active_supplier
        legacy_active = str(self.get("active_provider") or "").strip()
        if self.get_supplier(str(raw.get("active_supplier") or "")) is None:
            supplier = self.get_supplier(legacy_active) if legacy_active else None
            if supplier is None:
                supplier = self.active_supplier()
            if supplier is not None:
                raw["active_supplier"] = supplier.name
                self._cache["active_supplier"] = supplier.name
                changed = True

        if str(raw.get("active_protocol") or "").strip() == "":
            # 旧配置里 active_provider 指向的通道模板决定协议（Gemini / Grok 通道
            # 升级后不应该回落成 openai），取不到时再退回当前值。
            inferred = ""
            if legacy_active and legacy_active in legacy_protocols:
                inferred = legacy_protocols[legacy_active]
            raw["active_protocol"] = inferred or self.active_protocol()
            self._cache["active_protocol"] = raw["active_protocol"]
            changed = True

        # 清掉已被 active_supplier 取代的旧键，避免两处并存后出现理解歧义
        if "active_provider" in raw and str(raw.get("active_supplier") or "").strip():
            raw.pop("active_provider", None)
            changed = True

        return changed

    # ------------------------------------------------------------------ 尺寸
    def _write_sizes(self, sizes: list[str]) -> None:
        """同时写回 sizes / image_sizes / _sizes_sync，保证三个键完全一致。

        ``_sizes_sync`` 是内部同步标记：用于在下次 reload 时判断用户到底改了
        哪一个键（经典配置页只显示 image_sizes），从而避免读到旧值。
        """
        normalized = _as_str_list(sizes)
        self.set_raw("sizes", normalized)
        if isinstance(self._raw, dict):
            self._raw["image_sizes"] = list(normalized)
            self._raw["_sizes_sync"] = list(normalized)
        self._cache["image_sizes"] = list(normalized)
        self._cache["_sizes_sync"] = list(normalized)

    def image_sizes(self) -> list[str]:
        sizes = _as_str_list(self._cache.get("sizes"))
        result: list[str] = []
        for size in sizes:
            normalized = normalize_size(size, size)
            if normalized not in result:
                result.append(normalized)
        if not result:
            result = list(DEFAULT_SIZES)
        return result

    def active_size(self) -> str:
        size = str(self._cache.get("active_size") or "").strip()
        sizes = self.image_sizes()
        if size:
            normalized = normalize_size(size, size)
            if normalized in sizes or looks_like_size(normalized):
                return normalized
        return sizes[0] if sizes else DEFAULT_SIZE

    def set_active_size(self, size: str) -> bool:
        target = str(size or "").strip()
        if not target:
            return False
        target = normalize_size(target, target)
        if target not in self.image_sizes():
            self.add_size(target)
        elif target not in self._cache.get("sizes", []):
            pass
        self.set_raw("active_size", target)
        return True

    def add_size(self, size: str) -> bool:
        target = normalize_size(str(size or "").strip(), "")
        if not target:
            return False
        sizes = self.image_sizes()
        if target in sizes:
            return False
        sizes.append(target)
        self._write_sizes(sizes)
        return True

    def remove_size(self, size: str) -> bool:
        target = normalize_size(str(size or "").strip(), "")
        sizes = self.image_sizes()
        if target not in sizes or len(sizes) <= 1:
            return False
        sizes = [item for item in sizes if item != target]
        self._write_sizes(sizes)
        if self.active_size() == target:
            self.set_raw("active_size", sizes[0])
        return True

    # ------------------------------------------------------------------ 指令
    def draw_commands(self) -> list[str]:
        commands = _as_str_list(self._cache.get("draw_commands"))
        return commands or list(DEFAULT_DRAW_COMMANDS)

    def edit_commands(self) -> list[str]:
        commands = _as_str_list(self._cache.get("edit_commands"))
        return commands or list(DEFAULT_EDIT_COMMANDS)

    def menu_commands(self) -> list[str]:
        commands = _as_str_list(self._cache.get("menu_commands"))
        return commands or list(DEFAULT_MENU_COMMANDS)

    def master_commands(self) -> list[str]:
        commands = _as_str_list(self._cache.get("master_commands"))
        return commands or list(DEFAULT_MASTER_COMMANDS)

    def set_commands(self, key: str, values: Any) -> None:
        self.set_raw(key, _as_str_list(values))

    # ------------------------------------------------------------------ 文案
    def start_prompt(self) -> str:
        text = str(self._cache.get("start_prompt") or "").strip()
        return text or DEFAULT_START_PROMPT

    def done_prompt(self) -> str:
        text = str(self._cache.get("done_prompt") or "").strip()
        return text or DEFAULT_DONE_PROMPT

    def menu_text(self) -> str:
        text = str(self._cache.get("menu_text") or "").strip()
        # 旧版默认模板包含固定指令清单；升级后改为仅显示状态和用户自定义分组。
        if (
            "gpt-image-2.5绘画" in text
            and "{分组菜单}" in text
            and "快捷入口" in text
            and "主人指令" in text
        ):
            return DEFAULT_MENU_TEXT
        return text or DEFAULT_MENU_TEXT

    def set_prompt(self, key: str, value: str) -> None:
        self.set_raw(key, str(value or "").strip())

    # ------------------------------------------------------------------ 主人
    def masters(self) -> list[str]:
        return _as_str_list(self._cache.get("masters"))

    def add_master(self, user_id: str) -> bool:
        target = str(user_id or "").strip()
        if not target:
            return False
        masters = self.masters()
        if target in masters:
            return False
        masters.append(target)
        self.set_raw("masters", masters)
        return True

    def remove_master(self, user_id: str) -> bool:
        target = str(user_id or "").strip()
        masters = self.masters()
        if target not in masters:
            return False
        masters = [item for item in masters if item != target]
        self.set_raw("masters", masters)
        return True

    def masters_use_astrbot_admin(self) -> bool:
        return _as_bool(self._cache.get("masters_use_astrbot_admin"), True)

    def is_master(self, user_id: str, is_admin: bool = False) -> bool:
        target = str(user_id or "").strip()
        if target and target in self.masters():
            return True
        return bool(self.masters_use_astrbot_admin() and is_admin)

    # ------------------------------------------------------------------ 平台
    def enabled_platforms(self) -> list[str]:
        return _as_str_list(self._cache.get("enabled_platforms"))

    def enabled_bot_ids(self) -> list[str]:
        return _as_str_list(self._cache.get("enabled_bot_ids"))

    def is_platform_allowed(self, platform_name: str, platform_id: str = "") -> bool:
        allowed_types = self.enabled_platforms()
        if allowed_types and str(platform_name or "") not in allowed_types:
            return False
        allowed_ids = self.enabled_bot_ids()
        if allowed_ids and str(platform_id or "") not in allowed_ids:
            return False
        return True

    def set_platforms(self, platforms: Any) -> None:
        self.set_raw("enabled_platforms", _as_str_list(platforms))

    def set_bot_ids(self, bot_ids: Any) -> None:
        self.set_raw("enabled_bot_ids", _as_str_list(bot_ids))

    # ------------------------------------------------------------------ 群聊
    def private_enabled(self) -> bool:
        return _as_bool(self._cache.get("private_enabled"), True)

    def set_private_enabled(self, enabled: bool) -> None:
        self.set_raw("private_enabled", bool(enabled))

    def group_mode(self) -> str:
        mode = str(self._cache.get("group_mode") or "all").strip().lower()
        return mode if mode in {"all", "whitelist", "blacklist"} else "all"

    def set_group_mode(self, mode: str) -> bool:
        target = str(mode or "").strip().lower()
        if target not in {"all", "whitelist", "blacklist"}:
            return False
        self.set_raw("group_mode", target)
        return True

    def group_list(self) -> list[str]:
        return _as_str_list(self._cache.get("group_list"))

    def set_group_list(self, groups: Any) -> None:
        self.set_raw("group_list", _as_str_list(groups))

    def group_require_enable(self) -> bool:
        """需求 16：是否要求「群必须由主人显式开启」后才执行指令（默认 True）。"""
        return _as_bool_strict(self._cache.get("group_require_enable"), True)

    def set_group_require_enable(self, flag: bool) -> bool:
        """切换「群必须由主人开启」约束；返回是否真正发生变更。

        打开该约束时统一收敛为「白名单 = 已开启的群」：

        * 原来的 ``blacklist`` 表示「除名单外全部开放」，在新语义下无法等价表达，
          采用 fail-closed 策略清空名单，由主人重新逐个开启（旧的关闭意图得以保留）；
        * 原来的 ``all`` 同样转为空白名单，即所有群默认关闭。
        """
        target = bool(flag)
        if target == self.group_require_enable():
            return False
        if target and self.group_mode() != "whitelist":
            if self.group_mode() == "blacklist":
                self.set_raw("group_list", [])
            self.set_raw("group_mode", "whitelist")
        self.set_raw("group_require_enable", target)
        return True

    def is_group_allowed(self, group_id: str) -> bool:
        """判断某群 / 私聊是否允许使用插件。

        * ``group_id`` 为空（私聊）→ 返回 :meth:`private_enabled`
        * :meth:`group_require_enable` 为 True（默认）→ 仅当群在 ``group_list`` 中才放行
        * 否则沿用 ``all / whitelist / blacklist`` 旧语义
        """
        target = str(group_id or "").strip()
        if not target:
            return self.private_enabled()
        groups = self.group_list()
        if self.group_require_enable():
            return target in groups
        mode = self.group_mode()
        if mode == "whitelist":
            return target in groups
        if mode == "blacklist":
            return target not in groups
        return True

    def set_group_enabled(self, group_id: str, enabled: bool) -> bool:
        """切换某个群的开关；自动在 all / whitelist / blacklist 之间迁移。

        :meth:`group_require_enable` 为 True 时，``group_list`` 直接表示
        「已开启的群」，模式恒为 ``whitelist``。
        """
        target = str(group_id or "").strip()
        if not target:
            return False
        if self.group_require_enable():
            groups = self.group_list()
            if enabled:
                if target in groups:
                    return False
                groups.append(target)
                self.set_raw("group_list", groups)
                if self.group_mode() != "whitelist":
                    self.set_raw("group_mode", "whitelist")
                return True
            if target not in groups:
                return False
            self.set_raw("group_list", [item for item in groups if item != target])
            if self.group_mode() != "whitelist":
                self.set_raw("group_mode", "whitelist")
            return True
        mode = self.group_mode()
        groups = self.group_list()
        if mode == "all":
            if enabled:
                return False
            self.set_raw("group_mode", "blacklist")
            if target not in groups:
                groups.append(target)
            self.set_raw("group_list", groups)
            return True
        if mode == "blacklist":
            if enabled:
                groups = [item for item in groups if item != target]
                self.set_raw("group_list", groups)
                if not groups:
                    self.set_raw("group_mode", "all")
                return True
            if target in groups:
                return False
            groups.append(target)
            self.set_raw("group_list", groups)
            return True
        # whitelist
        if enabled:
            if target in groups:
                return False
            groups.append(target)
            self.set_raw("group_list", groups)
            return True
        groups = [item for item in groups if item != target]
        self.set_raw("group_list", groups)
        return True

    # ------------------------------------------------- 需求 2：机器人实例覆盖
    def bot_overrides(self) -> dict[str, dict[str, str]]:
        """返回全部机器人实例覆盖：``{平台实例ID: {supplier/protocol/model/size}}``。"""
        return _normalize_bot_overrides(self._cache.get("bot_overrides"))

    def bot_override(self, platform_id: str) -> dict[str, str]:
        """返回某个机器人实例的覆盖配置；未配置返回空字典。"""
        key = str(platform_id or "").strip()
        if not key:
            return {}
        return dict(self.bot_overrides().get(key) or {})

    def set_bot_override(self, platform_id: str, key: str, value: str) -> bool:
        """设置某个机器人实例的单项覆盖（字段白名单见 ``_BOT_OVERRIDE_FIELDS``）。

        ``value`` 传空串表示清除该项；全部字段清空后自动移除该实例。
        """
        platform = str(platform_id or "").strip()
        field = str(key or "").strip().lower()
        if not platform or field not in _BOT_OVERRIDE_FIELDS:
            return False
        overrides = self.bot_overrides()
        bucket = dict(overrides.get(platform) or {})
        text = str(value or "").strip()
        if text:
            bucket[field] = text
        else:
            bucket.pop(field, None)
        if bucket:
            overrides[platform] = bucket
        else:
            overrides.pop(platform, None)
        self.set_raw("bot_overrides", overrides)
        self._cache["bot_overrides"] = overrides
        return True

    def clear_bot_override(self, platform_id: str) -> bool:
        """清除某个机器人实例的全部覆盖配置。"""
        platform = str(platform_id or "").strip()
        overrides = self.bot_overrides()
        if not platform or platform not in overrides:
            return False
        overrides.pop(platform, None)
        self.set_raw("bot_overrides", overrides)
        self._cache["bot_overrides"] = overrides
        return True

    # --------------------------------------------- 需求 12：群友黑名单
    def user_blacklist(self) -> list[str]:
        """黑名单列表；条目为 ``用户ID`` 或 ``平台实例ID|用户ID``。"""
        return _as_id_list(self._cache.get("user_blacklist"))

    def add_user_blacklist(self, user_id: str) -> bool:
        target = str(user_id or "").strip()
        if not target:
            return False
        entries = self.user_blacklist()
        if target in entries:
            return False
        entries.append(target)
        self.set_raw("user_blacklist", entries)
        return True

    def remove_user_blacklist(self, user_id: str) -> bool:
        target = str(user_id or "").strip()
        entries = self.user_blacklist()
        if target not in entries:
            return False
        self.set_raw("user_blacklist", [item for item in entries if item != target])
        return True

    def is_user_blocked(self, user_id: str, platform_id: str = "") -> bool:
        """判断用户是否被拉黑；同时匹配「纯用户ID」与「平台实例ID|用户ID」。"""
        target = str(user_id or "").strip()
        if not target:
            return False
        platform = str(platform_id or "").strip()
        entries = self.user_blacklist()
        if platform and f"{platform}|{target}" in entries:
            return True
        return target in entries

    # --------------------------------------- 需求 14 / 17：内置提示词（预设）
    def _raw_prompts(self) -> list[dict[str, Any]]:
        """读取用户配置的提示词；None 表示从未自定义过（交由 prompts() 回落默认库）。"""
        value = self._cache.get("prompts")
        if value is None:
            return []
        return _normalize_prompt_list(value)

    def prompts(self) -> list[dict[str, Any]]:
        """返回提示词库；从未自定义过时回落到 ``core.presets.DEFAULT_PROMPTS``。"""
        if self._cache.get("prompts") is None:
            try:
                from .presets import DEFAULT_PROMPTS  # 延迟导入，避免循环依赖

                return _normalize_prompt_list(DEFAULT_PROMPTS)
            except Exception:  # noqa: BLE001 - presets 缺失时不应影响插件启动
                return []
        return self._raw_prompts()

    def prompt_names(self) -> list[str]:
        return [str(item.get("name") or "") for item in self.prompts()]

    def get_prompt(self, name: str) -> dict[str, Any] | None:
        """按名称 / 1-based 序号 / 唯一子串查找提示词；找不到返回 None。"""
        target = str(name or "").strip()
        entries = self.prompts()
        if not target or not entries:
            return None
        index, _value = resolve_choice(target, [str(item.get("name") or "") for item in entries])
        if index < 0:
            return None
        return dict(entries[index])

    def add_prompt(self, name: str, text: str, tags: Any = None) -> bool:
        """新增提示词；名称为空或重名返回 False。

        首次新增时会把内置提示词库固化进配置（此后即可用 :meth:`remove_prompt`
        删除内置预设，符合需求 17 的「可增可删」语义）。
        """
        entry = _normalize_prompt_entry({"name": name, "text": text, "tags": tags})
        if entry is None:
            return False
        entries = self.prompts()
        if entry["name"] in [str(item.get("name") or "") for item in entries]:
            return False
        entries.append(entry)
        entries = _normalize_prompt_list(entries)
        self.set_raw("prompts", entries)
        self._cache["prompts"] = entries
        return True

    def remove_prompt(self, name: str) -> bool:
        """删除提示词（含内置预设）；不存在返回 False。"""
        entry = self.get_prompt(name)
        if entry is None:
            return False
        target = str(entry.get("name") or "")
        entries = [item for item in self.prompts() if str(item.get("name") or "") != target]
        self.set_raw("prompts", entries)
        self._cache["prompts"] = entries
        return True

    def set_prompts(self, prompts: Any) -> None:
        """整体覆盖提示词库（设置页保存用；空列表表示清空自定义）。"""
        entries = _normalize_prompt_list(prompts)
        self.set_raw("prompts", entries)
        self._cache["prompts"] = entries

    # ------------------------------------- 需求 2：自定义菜单分组（可指令管理）
    def menu_sections(self) -> list[dict[str, Any]]:
        """返回用户菜单分组；未配置时回落，显式空列表保持为空。"""
        value = self._cache.get("menu_sections")
        if value is None:
            return _normalize_menu_sections(DEFAULT_MENU_SECTIONS)
        if value == []:
            return []
        entries = _normalize_menu_sections(value)
        # 清理旧版本自动写入的整套出厂分组，但保留用户自定义的其他分组。
        is_legacy_default = (
            len(entries) == len(_LEGACY_MENU_SECTION_NAMES)
            and tuple(item.get("name") for item in entries) == _LEGACY_MENU_SECTION_NAMES
            and all(
                tuple(item.get("items") or ()) == expected
                for item, expected in zip(entries, _LEGACY_MENU_SECTION_ITEMS)
            )
        )
        if is_legacy_default:
            return _normalize_menu_sections(DEFAULT_MENU_SECTIONS)
        return entries

    def menu_section_names(self) -> list[str]:
        return [str(item.get("name") or "") for item in self.menu_sections()]

    def get_menu_section(self, name: str) -> dict[str, Any] | None:
        """按名称 / 1-based 序号 / 唯一子串查找菜单分组；找不到返回 None。"""
        target = str(name or "").strip()
        entries = self.menu_sections()
        if not target or not entries:
            return None
        index, _value = resolve_choice(target, [str(item.get("name") or "") for item in entries])
        if index < 0:
            return None
        return dict(entries[index])

    def set_menu_sections(self, sections: Any) -> None:
        """整体覆盖菜单分组；空列表表示清空用户菜单。"""
        entries = _normalize_menu_sections(sections)
        self.set_raw("menu_sections", entries)
        self._cache["menu_sections"] = entries

    def set_menu_section(self, name: str, items: Any) -> bool:
        """新增或覆盖一个菜单分组。"""
        target = str(name or "").strip()
        if not target:
            return False
        entries = self.menu_sections()
        cleaned = _as_str_list(items)[:MENU_SECTION_ITEM_LIMIT]
        replaced = False
        result: list[dict[str, Any]] = []
        for item in entries:
            if str(item.get("name") or "") == target:
                result.append({"name": target, "items": cleaned})
                replaced = True
            else:
                result.append(dict(item))
        if not replaced:
            if len(result) >= MENU_SECTION_LIMIT:
                return False
            result.append({"name": target, "items": cleaned})
        self.set_menu_sections(result)
        return True

    def remove_menu_section(self, name: str) -> bool:
        """删除一个菜单分组；不存在返回 False。"""
        target = str(name or "").strip()
        entries = self.menu_sections()
        if not target or target not in [str(item.get("name") or "") for item in entries]:
            return False
        entries = [item for item in entries if str(item.get("name") or "") != target]
        self.set_menu_sections(entries)
        return True

    def reset_menu_sections(self) -> None:
        """恢复出厂菜单分组。"""
        self.set_menu_sections(DEFAULT_MENU_SECTIONS)

    # --------------------------------------- 需求 13：严格触发（防误触发）开关
    def strict_trigger(self) -> bool:
        """True=开启误触发保护（默认）；False=保持旧的前缀匹配行为。"""
        return _as_bool_strict(self._cache.get("strict_trigger"), True)

    def set_strict_trigger(self, flag: bool) -> None:
        self.set_raw("strict_trigger", bool(flag))

    # --------------------------------------------- 需求 3：批量出图上限
    def batch_max(self) -> int:
        value = _as_int(self._cache.get("batch_max"), BATCH_MAX_DEFAULT)
        return max(1, min(value, BATCH_MAX_LIMIT))

    def set_batch_max(self, value: Any) -> int:
        number = max(1, min(_as_int(value, BATCH_MAX_DEFAULT), BATCH_MAX_LIMIT))
        self.set_raw("batch_max", number)
        return number

    # ----------------------------------------- 需求 15：提示词尺寸识别
    def size_auto_detect(self) -> bool:
        return _as_bool_strict(self._cache.get("size_auto_detect"), True)

    def set_size_auto_detect(self, flag: bool) -> None:
        self.set_raw("size_auto_detect", bool(flag))

    # --------------------------------------------- 需求 8：每日抽卡
    def gacha_enabled(self) -> bool:
        return _as_bool_strict(self._cache.get("gacha_enabled"), True)

    def set_gacha_enabled(self, flag: bool) -> None:
        self.set_raw("gacha_enabled", bool(flag))

    def gacha_styles(self) -> list[dict[str, Any]]:
        """抽卡画风池；未自定义时回落到 ``core.presets.DEFAULT_GACHA_STYLES``。"""
        entries = _normalize_style_entries(self._cache.get("gacha_styles"))
        if entries:
            return entries
        try:
            from .presets import DEFAULT_GACHA_STYLES

            return _normalize_style_entries(DEFAULT_GACHA_STYLES)
        except Exception:  # noqa: BLE001
            return []

    def set_gacha_styles(self, styles: Any) -> None:
        entries = _normalize_style_entries(styles)
        self.set_raw("gacha_styles", entries)
        self._cache["gacha_styles"] = entries

    # --------------------------------------------- 需求 9：排行榜
    def rank_enabled(self) -> bool:
        return _as_bool_strict(self._cache.get("rank_enabled"), True)

    def set_rank_enabled(self, flag: bool) -> None:
        self.set_raw("rank_enabled", bool(flag))

    # --------------------------------------------- 需求 10：出图文案
    def flavor_enabled(self) -> bool:
        return _as_bool_strict(self._cache.get("flavor_enabled"), True)

    def set_flavor_enabled(self, flag: bool) -> None:
        self.set_raw("flavor_enabled", bool(flag))

    def flavor_lines(self) -> list[str]:
        """文案池；未自定义时回落到 ``core.presets.DEFAULT_FLAVOR_LINES``。"""
        entries = _normalize_text_list(self._cache.get("flavor_lines"))
        if entries:
            return entries
        try:
            from .presets import DEFAULT_FLAVOR_LINES

            return _normalize_text_list(DEFAULT_FLAVOR_LINES)
        except Exception:  # noqa: BLE001
            return []

    def set_flavor_lines(self, lines: Any) -> None:
        entries = _normalize_text_list(lines)
        self.set_raw("flavor_lines", entries)
        self._cache["flavor_lines"] = entries

    # --------------------------------------- 需求 11：图片转提示词
    def img2prompt_enabled(self) -> bool:
        return _as_bool_strict(self._cache.get("img2prompt_enabled"), False)

    def set_img2prompt_enabled(self, flag: bool) -> None:
        self.set_raw("img2prompt_enabled", bool(flag))

    def img2prompt_model(self) -> str:
        return _as_text(self._cache.get("img2prompt_model"))

    def set_img2prompt_model(self, model: str) -> None:
        self.set_raw("img2prompt_model", str(model or "").strip())

    # --------------------------------------- 需求 7：中文提示词翻译
    def translate_enabled(self) -> bool:
        return _as_bool_strict(self._cache.get("translate_enabled"), False)

    def set_translate_enabled(self, flag: bool) -> None:
        self.set_raw("translate_enabled", bool(flag))

    def translate_model(self) -> str:
        return _as_text(self._cache.get("translate_model"))

    def set_translate_model(self, model: str) -> None:
        self.set_raw("translate_model", str(model or "").strip())

    def translate_supplier(self) -> str:
        """翻译使用的供应商名；空串表示沿用当前供应商。"""
        return _as_text(self._cache.get("translate_supplier"))

    def set_translate_supplier(self, name: str) -> None:
        self.set_raw("translate_supplier", str(name or "").strip())

    def translate_prompt_text(self) -> str:
        """翻译用的系统提示词；未配置时返回内置默认文案。"""
        return _as_text(self._cache.get("translate_prompt_text"), DEFAULT_TRANSLATE_PROMPT)

    def set_translate_prompt_text(self, text: str) -> None:
        self.set_raw("translate_prompt_text", str(text or "").strip())

    # --------------------------------------- 需求 1：按协议过滤尺寸
    def size_list_for(self, protocol: str) -> list[str]:
        """返回某协议可用的尺寸列表（自动剔除不支持的尺寸，避免直接 400）。"""
        key = normalize_protocol(protocol) or self.active_protocol()
        try:
            return list(filter_sizes_for_protocol(key, self.image_sizes()))
        except Exception:  # noqa: BLE001 - 过滤失败时退回原始列表
            return list(self.image_sizes())

    def quality(self) -> str:
        value = str(self._cache.get("quality") or "auto").strip().lower()
        return value if value in {"auto", "low", "medium", "high", "xhigh", "max"} else "auto"

    def set_quality(self, value: Any) -> None:
        value = str(value or "auto").strip().lower()
        self.set_raw("quality", value if value in {"auto", "low", "medium", "high", "xhigh", "max"} else "auto")

    def transparent_background(self) -> bool:
        return _as_bool(self._cache.get("transparent_background"), False)

    def set_transparent_background(self, flag: Any) -> None:
        self.set_raw("transparent_background", _as_bool(flag, False))

    def qq_file_fallback(self) -> bool:
        return _as_bool(self._cache.get("qq_file_fallback"), True)

    def set_qq_file_fallback(self, flag: Any) -> None:
        self.set_raw("qq_file_fallback", _as_bool(flag, True))

    def auto_reload(self) -> bool:
        return _as_bool(self._cache.get("auto_reload"), True)

    def set_auto_reload(self, flag: Any) -> None:
        self.set_raw("auto_reload", _as_bool(flag, True))

    # ------------------------------------------------------------------ 其他
    def timeout(self) -> float:
        value = self._cache.get("timeout")
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            seconds = 600.0
        if seconds <= 0:
            seconds = 600.0
        return seconds

    def set_timeout(self, seconds: Any) -> None:
        try:
            value = float(seconds)
        except (TypeError, ValueError):
            value = 600.0
        if value <= 0:
            value = 600.0
        self.set_raw("timeout", int(value) if float(value).is_integer() else value)

    def max_input_images(self) -> int:
        value = _as_int(self._cache.get("max_input_images"), 6)
        return max(1, min(value, 20))

    def max_concurrent(self) -> int:
        value = _as_int(self._cache.get("max_concurrent"), 2)
        return max(1, min(value, 16))

    def reply_reference_image(self) -> bool:
        return _as_bool(self._cache.get("reply_reference_image"), True)

    def to_public_dict(self) -> dict[str, Any]:
        """给 WebUI 的安全视图（不含密钥明文）。"""
        suppliers = []
        for supplier in self.suppliers():
            suppliers.append(
                {
                    "name": supplier.name,
                    "base_url": supplier.base_url,
                    "api_key_set": bool(supplier.api_key),
                    "api_key_masked": _mask_api_key(supplier.api_key),
                }
            )
        active_protocol = self.active_protocol()
        return {
            "active_supplier": self.active_supplier_name(),
            "active_protocol": active_protocol,
            "protocol_label": PROTOCOL_SHORT_LABELS.get(active_protocol, active_protocol),
            "suppliers": suppliers,
            "protocol_models": self.protocol_models(),
            "model_cache": self._as_public_model_cache(),
            "trigger_mode": self.trigger_mode(),
            "cooldown": self.cooldown(),
            "retry_times": self.retry_times(),
            "proxy": self.proxy(),
            "supplier_commands": self.supplier_commands(),
            "active_provider": self.active_supplier_name(),
            "active_size": self.active_size(),
            "timeout": self.timeout(),
            "image_sizes": self.image_sizes(),
            "providers": suppliers,
            "protocols": [
                {"key": key, "label": PROTOCOL_LABELS.get(key, key)}
                for key in PROTOCOL_ORDER
            ],
            "draw_commands": self.draw_commands(),
            "edit_commands": self.edit_commands(),
            "menu_commands": self.menu_commands(),
            "master_commands": self.master_commands(),
            "start_prompt": self.start_prompt(),
            "done_prompt": self.done_prompt(),
            "menu_text": self.menu_text(),
            "masters": self.masters(),
            "masters_use_astrbot_admin": self.masters_use_astrbot_admin(),
            "enabled_platforms": self.enabled_platforms(),
            "enabled_bot_ids": self.enabled_bot_ids(),
            "private_enabled": self.private_enabled(),
            "group_mode": self.group_mode(),
            "group_list": self.group_list(),
            "max_input_images": self.max_input_images(),
            "max_concurrent": self.max_concurrent(),
            "reply_reference_image": self.reply_reference_image(),
            "group_require_enable": self.group_require_enable(),
            "user_blacklist": self.user_blacklist(),
            "bot_overrides": self.bot_overrides(),
            "prompts": self.prompts(),
            "prompt_names": self.prompt_names(),
            "batch_max": self.batch_max(),
            "size_auto_detect": self.size_auto_detect(),
            "gacha_enabled": self.gacha_enabled(),
            "gacha_styles": self.gacha_styles(),
            "rank_enabled": self.rank_enabled(),
            "flavor_enabled": self.flavor_enabled(),
            "flavor_lines": self.flavor_lines(),
            "img2prompt_enabled": self.img2prompt_enabled(),
            "img2prompt_model": self.img2prompt_model(),
            "translate_enabled": self.translate_enabled(),
            "translate_model": self.translate_model(),
            "translate_supplier": self.translate_supplier(),
            "translate_prompt_text": self.translate_prompt_text(),
            "menu_sections": self.menu_sections(),
            "menu_section_names": self.menu_section_names(),
            "strict_trigger": self.strict_trigger(),
            "quality": self.quality(),
            "transparent_background": self.transparent_background(),
            "qq_file_fallback": self.qq_file_fallback(),
            "auto_reload": self.auto_reload(),
            "size_list_for": {
                protocol: self.size_list_for(protocol)
                for protocol in PROTOCOL_ORDER
            },
        }

    def _as_public_model_cache(self) -> dict[str, list[str]]:
        """模型缓存的安全视图（按协议给出候选模型列表）。"""
        return {
            protocol: self.model_cache(protocol)
            for protocol in PROTOCOL_ORDER
        }


def resolve_data_dir(plugin_name: str = "astrbot_plugin_gpt_image") -> Path:
    """返回插件数据目录（data/plugin_data/<plugin_name>）。"""
    candidates: list[Path] = []
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        candidates.append(Path(get_astrbot_data_path()) / "plugin_data" / plugin_name)
    except Exception:  # noqa: BLE001
        pass
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        candidates.append(Path(get_astrbot_plugin_data_path()) / plugin_name)
    except Exception:  # noqa: BLE001
        pass
    env_path = os.environ.get("ASTRBOT_DATA_PATH")
    if env_path:
        candidates.append(Path(env_path) / "plugin_data" / plugin_name)
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:  # noqa: BLE001
            continue
    fallback = Path.cwd() / "data" / "plugin_data" / plugin_name
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


__all__ = [
    "BATCH_MAX_DEFAULT",
    "BATCH_MAX_LIMIT",
    "DEFAULT_CHANNELS",
    "DEFAULT_DONE_PROMPT",
    "DEFAULT_DRAW_COMMANDS",
    "DEFAULT_EDIT_COMMANDS",
    "DEFAULT_MASTER_COMMANDS",
    "DEFAULT_MENU_COMMANDS",
    "DEFAULT_MENU_TEXT",
    "DEFAULT_SIZES",
    "DEFAULT_START_PROMPT",
    "DEFAULT_SUPPLIERS",
    "DEFAULT_SUPPLIER_COMMANDS",
    "DEFAULT_SUPPLIER_NAME",
    "MODEL_CACHE_LIMIT",
    "DEFAULT_TRANSLATE_PROMPT",
    "PROMPT_LIMIT",
    "MENU_SECTION_LIMIT",
    "MENU_SECTION_ITEM_LIMIT",
    "DEFAULT_MENU_SECTIONS",
    "PROTOCOL_SHORT_LABELS",
    "PluginConfig",
    "TRIGGER_MODES",
    "resolve_data_dir",
]
