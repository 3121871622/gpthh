"""指令解析、文案渲染与菜单构建（纯逻辑模块，不依赖 astrbot）。

本模块只负责三件事：

1. parse_command(text, cfg)：把用户消息解析成 ParsedCommand，最长的指令名优先。
2. render_template(template, **kwargs)：渲染 {变量} 模板，中英文变量名互通。
3. build_menu(cfg, state_view, is_master=...)：生成菜单文本，超长自动截断。
4. render_numbered_list(title, items, current, page, page_size)：渲染带连续编号的列表。

接口约定见 _docs/CONTRACT.md 第 7 节。本模块除 core/models.py 的协议标签常量外
不引入其他依赖，也不 import astrbot，方便脱离框架单独测试。
"""

from __future__ import annotations

import re
from math import gcd
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:  # 作为插件包的一部分被导入（AstrBot 运行期）
    from .models import PROTOCOL_SHORT_LABELS
except ImportError:  # pragma: no cover - 兼容把 core/ 目录直接加入 sys.path 的测试场景
    try:
        from models import PROTOCOL_SHORT_LABELS  # type: ignore
    except ImportError:  # pragma: no cover
        PROTOCOL_SHORT_LABELS = {
            "openai": "OpenAI 兼容",
            "gemini": "Gemini 兼容",
            "grok": "Grok 兼容",
        }


# ==================================================================== 指令类型

KIND_DRAW = "draw"
KIND_EDIT = "edit"
KIND_MENU = "menu"
KIND_MODELS = "models"
KIND_SWITCH_MODEL = "switch_model"
KIND_SIZES = "sizes"
KIND_SWITCH_SIZE = "switch_size"
KIND_PROTOCOLS = "protocols"
KIND_SWITCH_PROTOCOL = "switch_protocol"
KIND_ADD_SIZE = "add_size"
KIND_DEL_SIZE = "del_size"
KIND_GROUPS = "groups"
KIND_GROUP_ON = "group_on"
KIND_GROUP_OFF = "group_off"
KIND_MASTER_ADD = "master_add"
KIND_MASTER_DEL = "master_del"
KIND_RELOAD = "reload"
KIND_HELP = "help"
KIND_WHOAMI = "whoami"
KIND_SUPPLIERS = "suppliers"
KIND_SWITCH_SUPPLIER = "switch_supplier"
KIND_STATS = "stats"
KIND_RESET = "reset"
KIND_PRESET_LIST = "preset_list"
KIND_PRESET_ADD = "preset_add"
KIND_PRESET_DEL = "preset_del"
KIND_GACHA = "gacha"
KIND_RANK = "rank"
KIND_IMG2PROMPT = "img2prompt"
KIND_REPEAT = "repeat"
KIND_MENU_LIST = "menu_list"
KIND_MENU_SET = "menu_set"
KIND_MENU_DEL = "menu_del"
KIND_MENU_SHOW = "menu_show"
KIND_PROMPT_TOOLS = "prompt_tools"
KIND_UPDATE = "update"

ALL_KINDS: Tuple[str, ...] = (
    KIND_DRAW,
    KIND_EDIT,
    KIND_MENU,
    KIND_MODELS,
    KIND_SWITCH_MODEL,
    KIND_SIZES,
    KIND_SWITCH_SIZE,
    KIND_PROTOCOLS,
    KIND_SWITCH_PROTOCOL,
    KIND_ADD_SIZE,
    KIND_DEL_SIZE,
    KIND_GROUPS,
    KIND_GROUP_ON,
    KIND_GROUP_OFF,
    KIND_MASTER_ADD,
    KIND_MASTER_DEL,
    KIND_RELOAD,
    KIND_HELP,
    KIND_WHOAMI,
    KIND_SUPPLIERS,
    KIND_SWITCH_SUPPLIER,
    KIND_STATS,
    KIND_RESET,
    KIND_PRESET_LIST,
    KIND_PRESET_ADD,
    KIND_PRESET_DEL,
    KIND_GACHA,
    KIND_RANK,
    KIND_IMG2PROMPT,
    KIND_REPEAT,
    KIND_MENU_LIST,
    KIND_MENU_SET,
    KIND_MENU_DEL,
    KIND_MENU_SHOW,
    KIND_PROMPT_TOOLS,
    KIND_UPDATE,
)

#: 需要把剩余文本当作绘画/编辑内容的指令类型
CONTENT_KINDS: Tuple[str, ...] = (
    KIND_DRAW,
    KIND_EDIT,
    KIND_PRESET_ADD,
    KIND_PRESET_DEL,
    KIND_MENU_SET,
)

QUERY_KINDS: Tuple[str, ...] = tuple(
    kind for kind in ALL_KINDS if kind not in CONTENT_KINDS
)

#: 需要主人权限的指令类型（具体校验在调用方完成）
MASTER_KINDS: Tuple[str, ...] = (
    KIND_PRESET_ADD,
    KIND_PRESET_DEL,
    KIND_SWITCH_MODEL,
    KIND_SWITCH_SIZE,
    KIND_SWITCH_PROTOCOL,
    KIND_SWITCH_SUPPLIER,
    KIND_ADD_SIZE,
    KIND_DEL_SIZE,
    KIND_GROUPS,
    KIND_GROUP_ON,
    KIND_GROUP_OFF,
    KIND_MASTER_ADD,
    KIND_MASTER_DEL,
    KIND_RELOAD,
    KIND_STATS,
    KIND_RESET,
    KIND_MENU_SET,
    KIND_MENU_DEL,
)


@dataclass
class ParsedCommand:
    """一条被解析出来的指令。

    :param kind: 指令类型，取值为本模块的 KIND_* 常量。
    :param arg: 指令名之后的剩余文本（绘画/编辑类为内容，其余为参数）。
    :param raw: 用户发送的原始文本。
    :param command: 实际命中的指令名（用户可能配置了多个别名）。
    """

    kind: str
    arg: str = ""
    raw: str = ""
    command: str = ""


# ====================================================== 配置读取与出厂内置别名

# 下面这些默认值与 core/config.py 中的 DEFAULT_* 常量保持一致，仅作为
# 「传入的 cfg 缺方法 / 返回空」时的兜底，正常情况下由 PluginConfig 提供。
_FALLBACK_DRAW_COMMANDS: Tuple[str, ...] = ("绘画", "画图")
_FALLBACK_EDIT_COMMANDS: Tuple[str, ...] = ("图片编辑", "编辑图片")
_FALLBACK_MENU_COMMANDS: Tuple[str, ...] = ("绘画菜单", "菜单")

# 未配置菜单分组时的出厂菜单。只引用公开指令，不包含表情或隐藏内容。
_DEFAULT_MENU_SECTIONS: Tuple[dict, ...] = (
    {"name": "基础创作", "items": ("绘画", "图片编辑", "重画", "使用说明", "我的ID")},
    {
        "name": "模型与供应商",
        "items": ("供应商列表", "切换供应商", "协议列表", "切换协议", "模型列表", "切换模型"),
    },
    {
        "name": "尺寸与提示词",
        "items": ("尺寸列表", "切换尺寸", "图片转提示词", "预设列表", "提示词工具"),
    },
    {"name": "群组与权限", "items": ("群列表", "群开关", "我的ID", "添加主人", "删除主人")},
    {"name": "娱乐互动", "items": ("抽卡", "排行", "重画")},
    {
        "name": "管理维护",
        "items": ("菜单列表", "设置菜单", "删除菜单", "运行统计", "重载配置", "重置设置"),
    },
)
_FALLBACK_MASTER_COMMANDS: Tuple[str, ...] = (
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
)

#: 供应商 / 协议 / 统计类指令的兜底名（与 core/config.py 的 DEFAULT_SUPPLIER_COMMANDS 一致）
_FALLBACK_SUPPLIER_COMMANDS: Tuple[str, ...] = (
    "供应商列表",
    "切换供应商",
    "协议列表",
    "切换协议",
    "模型列表",
    "切换模型",
    "运行统计",
    "重置设置",
)

#: 主人指令名 -> 指令类型（按契约写死的映射）
_MASTER_COMMAND_KINDS: Dict[str, str] = {
    "切换模型": KIND_SWITCH_MODEL,
    "切换尺寸": KIND_SWITCH_SIZE,
    "切换协议": KIND_SWITCH_PROTOCOL,
    "切换供应商": KIND_SWITCH_SUPPLIER,
    "添加尺寸": KIND_ADD_SIZE,
    "删除尺寸": KIND_DEL_SIZE,
    "群开关": KIND_GROUPS,
    "添加主人": KIND_MASTER_ADD,
    "删除主人": KIND_MASTER_DEL,
    "重载配置": KIND_RELOAD,
    "运行统计": KIND_STATS,
    "重置设置": KIND_RESET,
    "添加预设": KIND_PRESET_ADD,
    "删除预设": KIND_PRESET_DEL,
    "预设列表": KIND_PRESET_LIST,
    "图片转提示词": KIND_IMG2PROMPT,
    "设置菜单": KIND_MENU_SET,
    "删除菜单": KIND_MENU_DEL,
}

_MASTER_COMMAND_KINDS_LOWER: Dict[str, str] = {
    name.lower(): kind for name, kind in _MASTER_COMMAND_KINDS.items()
}

#: 主人指令按默认顺序的兜底映射：用户改了指令名时仍能对应上功能
_MASTER_KINDS_BY_INDEX: Tuple[str, ...] = tuple(
    _MASTER_COMMAND_KINDS.get(name, "") for name in _FALLBACK_MASTER_COMMANDS
)

#: 供应商 / 协议 / 模型列表类指令的默认名 -> 指令类型（与 core/config.py 的
#: DEFAULT_SUPPLIER_COMMANDS、pages/settings 的契约顺序一一对应）
_SUPPLIER_COMMAND_KINDS: Dict[str, str] = {
    "供应商列表": KIND_SUPPLIERS,
    "切换供应商": KIND_SWITCH_SUPPLIER,
    "协议列表": KIND_PROTOCOLS,
    "切换协议": KIND_SWITCH_PROTOCOL,
    "模型列表": KIND_MODELS,
    "切换模型": KIND_SWITCH_MODEL,
    "运行统计": KIND_STATS,
    "重置设置": KIND_RESET,
}

_SUPPLIER_COMMAND_KINDS_LOWER: Dict[str, str] = {
    name.lower(): kind for name, kind in _SUPPLIER_COMMAND_KINDS.items()
}

#: 供应商 / 协议 / 模型列表类指令按默认顺序的类型映射（恒为 8 项）
_SUPPLIER_COMMAND_KINDS_BY_INDEX: Tuple[str, ...] = tuple(
    _SUPPLIER_COMMAND_KINDS.get(name, "") for name in _FALLBACK_SUPPLIER_COMMANDS
)

#: 出厂内置别名（不依赖用户配置，是否放行由调用方决定）
_BUILTIN_ALIASES: Tuple[Tuple[str, str], ...] = (
    ("模型列表", KIND_MODELS),
    ("模型菜单", KIND_MODELS),
    ("尺寸列表", KIND_SIZES),
    ("尺寸菜单", KIND_SIZES),
    ("协议列表", KIND_PROTOCOLS),
    ("协议菜单", KIND_PROTOCOLS),
    ("接口列表", KIND_PROTOCOLS),
    ("供应商列表", KIND_SUPPLIERS),
    ("供应商菜单", KIND_SUPPLIERS),
    ("运行统计", KIND_STATS),
    ("统计", KIND_STATS),
    ("重置设置", KIND_RESET),
    ("恢复默认设置", KIND_RESET),
    ("群列表", KIND_GROUPS),
    ("开群", KIND_GROUP_ON),
    ("开启本群", KIND_GROUP_ON),
    ("关群", KIND_GROUP_OFF),
    ("关闭本群", KIND_GROUP_OFF),
    ("帮助", KIND_HELP),
    ("使用说明", KIND_HELP),
    ("菜单帮助", KIND_HELP),
    ("我的ID", KIND_WHOAMI),
    ("查询ID", KIND_WHOAMI),
    ("我的信息", KIND_WHOAMI),
    ("我的id", KIND_WHOAMI),
    ("抽卡", KIND_GACHA),
    ("盲盒", KIND_GACHA),
    ("榜单", KIND_RANK),
    ("绘画榜", KIND_RANK),
    ("排行榜", KIND_RANK),
    ("预设", KIND_PRESET_LIST),
    ("预设列表", KIND_PRESET_LIST),
    ("提示词列表", KIND_PRESET_LIST),
    ("菜单列表", KIND_MENU_LIST),
    ("全部菜单", KIND_MENU_LIST),
    ("图片转提示词", KIND_IMG2PROMPT),
    ("图生文", KIND_IMG2PROMPT),
    ("反推提示词", KIND_IMG2PROMPT),
    ("提示词工具", KIND_PROMPT_TOOLS),
    ("重画", KIND_REPEAT),
    ("再来一张", KIND_REPEAT),
)

#: 同名指令的优先级：数字越小越优先（菜单/内置类 > 主人指令 > 编辑 > 绘画）
_KIND_PRIORITY: Dict[str, int] = {
    KIND_MENU: 0,
    KIND_HELP: 1,
    KIND_MODELS: 1,
    KIND_SIZES: 1,
    KIND_PROTOCOLS: 1,
    KIND_SUPPLIERS: 1,
    KIND_STATS: 1,
    KIND_RESET: 1,
    KIND_GROUPS: 1,
    KIND_RELOAD: 1,
    KIND_WHOAMI: 1,
    KIND_PRESET_LIST: 1,
    KIND_RANK: 1,
    KIND_GACHA: 1,
    KIND_PRESET_ADD: 2,
    KIND_PRESET_DEL: 2,
    KIND_MENU_LIST: 1,
    KIND_PROMPT_TOOLS: 1,
    KIND_UPDATE: 1,
    KIND_MENU_SET: 2,
    KIND_MENU_DEL: 2,
    KIND_MENU_SHOW: 2,
    KIND_IMG2PROMPT: 2,
    KIND_REPEAT: 4,
    KIND_SWITCH_MODEL: 2,
    KIND_SWITCH_SIZE: 2,
    KIND_SWITCH_PROTOCOL: 2,
    KIND_SWITCH_SUPPLIER: 2,
    KIND_ADD_SIZE: 2,
    KIND_DEL_SIZE: 2,
    KIND_GROUP_ON: 2,
    KIND_GROUP_OFF: 2,
    KIND_MASTER_ADD: 2,
    KIND_MASTER_DEL: 2,
    KIND_EDIT: 3,
    KIND_DRAW: 4,
}

_WHITESPACE_RE = re.compile(r"\s+")
_VARIABLE_RE = re.compile(r"\{([^{}]+)\}")
_ESCAPE_LBRACE = "\x00"
_ESCAPE_RBRACE = "\x01"

#: 菜单长度上限（超出后截断并加省略号）
MENU_MAX_LENGTH = 2000


def _as_str_list(value: Any) -> List[str]:
    """把配置里的列表字段统一成去重后的字符串列表。"""
    items: List[Any]
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        items = re.split(r"[,，、\n]+", text)
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        items = [value]
    result: List[str] = []
    for item in items:
        name = str(item or "").strip()
        if name and name not in result:
            result.append(name)
    return result


def _cfg_list(cfg: Any, method: str, fallback: Sequence[str]) -> List[str]:
    """读取 cfg 上的列表方法；缺失或为空时回退到出厂默认值。"""
    getter = getattr(cfg, method, None)
    if callable(getter):
        try:
            values = getter()
        except Exception:  # noqa: BLE001 - 配置异常不应影响解析
            values = None
        names = _as_str_list(values)
        if names:
            return names
    return [str(item) for item in fallback]


def _cfg_text(cfg: Any, method: str, fallback: str = "") -> str:
    """读取 cfg 上的文本方法；缺失时回退。"""
    getter = getattr(cfg, method, None)
    if callable(getter):
        try:
            value = getter()
        except Exception:  # noqa: BLE001
            value = None
        if value is not None:
            return str(value)
    return fallback


def _master_kind_by_index(index: int) -> str:
    """主人指令名被用户改写时，按默认顺序推断其类型。"""
    if 0 <= index < len(_MASTER_KINDS_BY_INDEX):
        return _MASTER_KINDS_BY_INDEX[index]
    return ""


def _is_subsequence(needle: str, haystack: str) -> bool:
    """判断 needle 是否为 haystack 的子序列（字符按顺序出现，允许间隔）。

    例如「删尺寸」是「删除尺寸」的子序列，用于识别用户把指令名写短了的情况。
    """
    if not needle or not haystack:
        return False
    position = 0
    for char in haystack:
        if position < len(needle) and needle[position] == char:
            position += 1
            if position == len(needle):
                return True
    return False


def _kind_by_containment(name: str, table: Dict[str, str]) -> str:
    """按「唯一包含」关系保守推断类型，用于用户增删指令后的兜底。

    判定顺序：先看互为子串（例如「协议」包含于「协议列表」），再看子序列
    （例如「删尺寸」是「删除尺寸」的子序列）。默认名之间互不包含，所以
    同一份默认表里通常最多命中一个；命中多个说明信息不足，返回空串放弃
    推断——宁可该指令不可用，也不能把功能错位成另一个。
    """
    text = str(name or "").strip().lower()
    if not text:
        return ""
    matched_substring: List[str] = []
    matched_sequence: List[str] = []
    for default_name, kind in table.items():
        if not kind:
            continue
        if default_name in text or text in default_name:
            if kind not in matched_substring:
                matched_substring.append(kind)
        elif len(text) >= 2 and len(text) <= len(default_name) and _is_subsequence(text, default_name):
            if kind not in matched_sequence:
                matched_sequence.append(kind)
    matched = matched_substring or matched_sequence
    if len(matched) == 1:
        return matched[0]
    return ""


def _master_kind_for(name: str, index: int, names: Sequence[str]) -> str:
    """推断用户自定义主人指令名对应的类型。

    规则（按优先级）：

    1. 命中出厂默认名（大小写不敏感）时直接返回；
    2. 与某个默认名构成「唯一包含 / 子序列」关系时返回对应类型；
    3. 用户列表长度与默认列表一致，说明只是改名、没有增删，这时才按位置
       兜底（用户可能调换过顺序，所以放在包含匹配之后）；
    4. 都不成立时返回空串，该名字不会被注册成可解析指令——避免用户增删
       指令后，按位置推断把功能错位成另一个。
    """
    key = str(name or "").strip().lower()
    exact = _MASTER_COMMAND_KINDS_LOWER.get(key)
    if exact:
        return exact
    guessed = _kind_by_containment(name, _MASTER_COMMAND_KINDS_LOWER)
    if guessed:
        return guessed
    # 位置兜底：仅在配置项数与默认项数完全一致时启用（说明用户只是改名）。
    # 严禁在长度不一致时按位置猜测——「切换」「尺寸」这类短名会命中多项，
    # 猜错会把功能错位成另一个，宁可该指令不可用。
    if len(names) == len(_MASTER_KINDS_BY_INDEX):
        kind = _master_kind_by_index(index)
        if kind:
            return kind
    return ""


def _supplier_kind_by_index(index: int) -> str:
    """供应商类指令名被用户改写时，按契约顺序推断其类型（越界返回空串）。"""
    if 0 <= index < len(_SUPPLIER_COMMAND_KINDS_BY_INDEX):
        return _SUPPLIER_COMMAND_KINDS_BY_INDEX[index]
    return ""


def _supplier_kind_for(name: str, index: int, names: Sequence[str]) -> str:
    """推断供应商 / 协议 / 模型列表类指令名对应的类型。

    规则（按优先级）：

    1. 命中出厂默认名（大小写不敏感）时直接返回；
    2. 供应商类指令是「固定槽位」契约——core/config.py 的 PluginConfig
       会把 supplier_commands 截断 / 补齐成 8 项，设置页也按同样顺序展示，
       所以长度一致时按位置兜底是可靠的；
    3. 长度异常（理论上不会出现）时退回「唯一包含 / 子序列」匹配，仍无法
       唯一确定就返回空串，避免类型串位。
    """
    key = str(name or "").strip().lower()
    exact = _SUPPLIER_COMMAND_KINDS_LOWER.get(key)
    if exact:
        return exact
    if len(names) == len(_SUPPLIER_COMMAND_KINDS_BY_INDEX):
        return _supplier_kind_by_index(index)
    return _kind_by_containment(name, _SUPPLIER_COMMAND_KINDS_LOWER)


def _command_candidates(cfg: Any) -> List[Tuple[str, str]]:
    """收集全部候选指令，按「长指令优先」排序后返回 (指令名, 类型)。

    同一轮匹配中同时包含用户配置的指令与出厂内置别名，保证「绘画菜单」这类
    更长的指令不会被「绘画」抢先命中。
    """
    items: List[Tuple[str, str, int]] = []
    order = 0

    def add(values: Iterable[Any], kind: str) -> None:
        nonlocal order
        for value in values:
            name = str(value or "").strip()
            if name:
                items.append((name, kind, order))
                order += 1

    add(_cfg_list(cfg, "menu_commands", _FALLBACK_MENU_COMMANDS), KIND_MENU)
    for name, kind in _BUILTIN_ALIASES:
        add([name], kind)
    master_names = _cfg_list(cfg, "master_commands", _FALLBACK_MASTER_COMMANDS)
    for index, name in enumerate(master_names):
        kind = _master_kind_for(name, index, master_names)
        if kind:
            add([name], kind)
    supplier_names = _cfg_list(cfg, "supplier_commands", _FALLBACK_SUPPLIER_COMMANDS)
    for index, name in enumerate(supplier_names):
        kind = _supplier_kind_for(name, index, supplier_names)
        if kind:
            add([name], kind)
    add(_cfg_list(cfg, "edit_commands", _FALLBACK_EDIT_COMMANDS), KIND_EDIT)
    add(_cfg_list(cfg, "draw_commands", _FALLBACK_DRAW_COMMANDS), KIND_DRAW)

    # 菜单是显式导航入口，必须先于绘画、编辑及自动幽默等内容型指令。
    # 先按类型优先级，再按长度排序，避免同名自定义菜单被功能指令抢走。
    ordered = sorted(
        items,
        key=lambda item: (_KIND_PRIORITY.get(item[1], 9), -len(item[0]), item[2]),
    )
    # 同名指令可能在多处出现（例如「运行统计」既是出厂别名又写在用户列表里）。
    # 排序后保留最靠前的一条即可：顺序本身已表达「长指令优先、同类优先级」。
    result: List[Tuple[str, str]] = []
    seen: set = set()
    for name, kind, _ in ordered:
        if name in seen:
            continue
        seen.add(name)
        result.append((name, kind))
    return result


def all_command_names(cfg: Any) -> List[str]:
    """返回所有可用指令名（去重、长指令在前），供 main.py 注册入口使用。"""
    return [name for name, _ in _command_candidates(cfg)]


def is_master_kind(kind: str) -> bool:
    """判断某个指令类型是否需要主人权限。"""
    return kind in MASTER_KINDS


# ==================================================================== 文案渲染

#: 中英文变量名 -> 规范键。canonical 与中文名都能在模板里使用。
_ALIASES: Dict[str, str] = {
    # 时间与数量
    "耗时": "elapsed",
    "elapsed": "elapsed",
    "elapsed_time": "elapsed",
    "elapsed_seconds": "elapsed",
    "duration": "elapsed",
    "duration_seconds": "elapsed",
    "用时": "elapsed",
    "seconds": "elapsed",
    "数量": "count",
    "count": "count",
    "张数": "count",
    "图片数量": "count",
    "image_count": "count",
    "total_count": "count",
    "success_count": "success_count",
    "成功数量": "success_count",
    "成功数": "success_count",
    "failure_count": "failed_count",
    "失败数": "failed_count",
    "失败原因": "failure_reason",
    "failure_reason": "failure_reason",
    "错误": "failure_reason",
    "retry_count": "retry_count",
    "重试次数": "retry_count",
    "attempt": "attempt",
    "尝试次数": "attempt",
    "elapsed_ms": "elapsed_ms",
    "耗时毫秒": "elapsed_ms",
    "started_at": "started_at",
    "开始时间": "started_at",
    "finished_at": "finished_at",
    "完成时间": "finished_at",
    "task_id": "task_id",
    "任务ID": "task_id",
    "任务编号": "task_id",
    "request_id": "task_id",
    "请求ID": "task_id",
    "timeout": "timeout",
    "超时": "timeout",
    "超时时间": "timeout",
    "time": "time",
    "时间": "time",
    "当前时间": "time",
    "timestamp": "time",
    "date": "date",
    "日期": "date",
    "request_time": "request_time",
    "请求时间": "request_time",
    "year": "year",
    "年份": "year",
    "month": "month",
    "月份": "month",
    "day": "day",
    "日": "day",
    "weekday": "weekday",
    "星期": "weekday",
    # 模型与尺寸
    "model": "model",
    "模型": "model",
    "模型名称": "model",
    "size": "size",
    "尺寸": "size",
    "图片尺寸": "size",
    "current_model": "current_model",
    "当前模型": "current_model",
    "model_name": "model",
    "模型名": "model",
    "current_size": "current_size",
    "当前尺寸": "current_size",
    # 通道与协议
    "provider": "provider",
    "通道": "provider",
    "通道名": "provider",
    "接口通道": "provider",
    "渠道": "provider",
    "channel": "channel",
    "通道实例": "channel",
    "protocol": "protocol",
    "协议": "protocol",
    "接口协议": "protocol",
    "protocol_key": "protocol_key",
    "协议键": "protocol_key",
    "protocol_name": "protocol_label",
    "协议显示名": "protocol_label",
    # 内容
    "prompt": "prompt",
    "提示词": "prompt",
    "提示语": "prompt",
    "内容": "prompt",
    "original_prompt": "original_prompt",
    "raw_prompt": "original_prompt",
    "原始提示词": "original_prompt",
    "final_prompt": "final_prompt",
    "最终提示词": "final_prompt",
    "width": "width",
    "宽度": "width",
    "height": "height",
    "高度": "height",
    "mode": "mode",
    "模式": "mode",
    "batch_count": "batch_count",
    "batch_total": "batch_count",
    "request_count": "batch_count",
    "requested_count": "batch_count",
    "total_requests": "batch_count",
    "batch": "batch_count",
    "批量数量": "batch_count",
    "批量": "batch_count",
    "input_count": "input_count",
    "输入图片数量": "input_count",
    "输入数量": "input_count",
    "input_image_count": "input_count",
    "输入图片数": "input_count",
    "failed_count": "failed_count",
    "失败数量": "failed_count",
    "失败数": "failed_count",
    "prompt_type": "prompt_type",
    "类型": "prompt_type",
    "操作": "prompt_type",
    "image_count": "count",
    "图片数": "count",
    "output_count": "count",
    "输出数量": "count",
    "result_count": "count",
    "结果数量": "count",
    "remaining_count": "remaining_count",
    "剩余数量": "remaining_count",
    "has_images": "has_images",
    "是否有图片": "has_images",
    "prompt_length": "prompt_length",
    "提示词长度": "prompt_length",
    "success_rate": "success_rate",
    "成功率": "success_rate",
    "attempts_allowed": "attempts_allowed",
    "最大尝试次数": "attempts_allowed",
    "retry_limit": "retry_limit",
    "重试上限": "retry_limit",
    "failure_message": "failure_reason",
    "失败信息": "failure_reason",
    "generation_mode": "mode",
    "生成模式": "mode",
    "is_edit": "is_edit",
    "是否编辑": "is_edit",
    "plugin_version": "plugin_version",
    "插件版本": "plugin_version",
    # 用户与消息
    "user": "user",
    "用户": "user",
    "用户名": "user",
    "昵称": "user",
    "user_id": "user_id",
    "用户ID": "user_id",
    "用户id": "user_id",
    "用户编号": "user_id",
    "用户标识": "user_id",
    "message": "message",
    "消息": "message",
    "说明": "message",
    "提示": "message",
    "状态": "status",
    "status": "status",
    "command": "command",
    "指令": "command",
    "menu": "menu",
    "菜单": "menu",
    "group_id": "group_id",
    "群号": "group_id",
    "群ID": "group_id",
    "群id": "group_id",
    "group": "group_id",
    "group_name": "group_name",
    "群名": "group_name",
    "群名称": "group_name",
    # 平台与机器人实例
    "platform": "platform",
    "平台": "platform",
    "平台类型": "platform",
    "platform_name": "platform",
    "adapter": "platform",
    "适配器": "platform",
    "bot_instance": "bot_instance",
    "机器人实例": "bot_instance",
    "机器人实例ID": "bot_instance",
    "bot_instance_id": "bot_instance",
    "bot_id": "bot_instance",
    "platform_id": "bot_instance",
    "机器人ID": "bot_instance",
    "platform_instance": "bot_instance",
    "平台实例": "bot_instance",
    "平台实例ID": "bot_instance",
    "message_id": "message_id",
    "消息ID": "message_id",
    "session_id": "session_id",
    "会话ID": "session_id",
    "session": "session_id",
    "会话": "session_id",
    "conversation_id": "session_id",
    "会话编号": "session_id",
    "user_name": "user",
    "用户名称": "user",
    "nickname": "user",
    "用户昵称": "user",
    # 原始消息与原始文本；message 保留为已有的通用消息变量
    "raw_message": "raw_message",
    "original_message": "raw_message",
    "raw_text": "raw_message",
    "raw": "raw_message",
    "原始消息": "raw_message",
    "原始文本": "raw_message",
    "原文消息": "raw_message",
    "群模式": "group_mode",
    "group_mode": "group_mode",
    "群状态": "group_status",
    "group_status": "group_status",
    "group_enabled": "group_enabled",
    "是否开启": "group_enabled",
    "is_group": "is_group",
    "是否群聊": "is_group",
    "is_private": "is_private",
    "是否私聊": "is_private",
    "冷却": "cooldown",
    "cooldown": "cooldown",
    "触发方式": "trigger_mode",
    "trigger_mode": "trigger_mode",
    "supplier": "supplier",
    "供应商": "supplier",
    "供应商名": "supplier",
    "供应商名称": "supplier",
    "supplier_name": "supplier",
    "provider_name": "supplier",
    "supplier_index": "supplier_index",
    "供应商序号": "supplier_index",
    "supplier_total": "supplier_total",
    "供应商总数": "supplier_total",
    "protocol_index": "protocol_index",
    "协议序号": "protocol_index",
    "model_index": "model_index",
    "模型序号": "model_index",
    "model_total": "model_total",
    "模型总数": "model_total",
    # 菜单相关
    "draw_command": "draw_command",
    "绘画指令": "draw_command",
    "edit_command": "edit_command",
    "编辑指令": "edit_command",
    "menu_command": "menu_command",
    "菜单指令": "menu_command",
    "supplier_menu": "supplier_menu",
    "供应商菜单": "supplier_menu",
    "stats_command": "stats_command",
    "统计指令": "stats_command",
    "reset_command": "reset_command",
    "重置指令": "reset_command",
    "model_menu": "model_menu",
    "模型菜单": "model_menu",
    "size_menu": "size_menu",
    "尺寸菜单": "size_menu",
    "protocol_menu": "protocol_menu",
    "协议菜单": "protocol_menu",
    "protocol_label": "protocol_label",
    "协议名称": "protocol_label",
    "group_menu": "group_menu",
    "群列表": "group_menu",
    "help_command": "help_command",
    "帮助指令": "help_command",
    "帮助": "help_command",
    "master_commands": "master_commands",
    "主人指令": "master_commands",
    "draw_commands": "draw_commands",
    "绘画指令表": "draw_commands",
    "edit_commands": "edit_commands",
    "编辑指令表": "edit_commands",
    "menu_commands": "menu_commands",
    "菜单指令表": "menu_commands",
    "menu_sections": "menu_sections",
    "分组菜单": "menu_sections",
    "菜单分组": "menu_sections",
    "menu_list": "menu_sections",
    # 由现有上下文安全推导的便捷变量
    "batch_index": "batch_index",
    "batch_position": "batch_index",
    "request_index": "batch_index",
    "request_position": "batch_index",
    "批量位置": "batch_index",
    "请求位置": "batch_index",
    "datetime": "datetime",
    "date_time": "datetime",
    "日期时间": "datetime",
    "current_datetime": "datetime",
    "aspect_ratio": "aspect_ratio",
    "ratio": "aspect_ratio",
    "画面比例": "aspect_ratio",
    "pixel_count": "pixel_count",
    "pixels": "pixel_count",
    "像素数": "pixel_count",
    "is_square": "is_square",
    "是否正方形": "is_square",
    "is_batch": "is_batch",
    "是否批量": "is_batch",
    "has_success": "has_success",
    "是否有成功": "has_success",
    "has_failure": "has_failure",
    "是否有失败": "has_failure",
}

#: 规范键缺失时的兜底键（例如只传了 model 也要能渲染出 {当前模型}）
_ALIAS_FALLBACKS: Dict[str, Tuple[str, ...]] = {
    "current_model": ("model",),
    "current_size": ("size",),
    "channel": ("provider",),
    "supplier": ("provider", "channel", "channel_name"),
    "protocol_label": ("protocol",),
    "protocol_key": ("protocol",),
    "draw_commands": ("draw_command",),
    "edit_commands": ("edit_command",),
    "menu_commands": ("menu_command",),
    "master_commands": ("command",),
    "bot_instance": ("bot_id", "platform_instance", "platform_id"),
    "success_count": ("count",),
    "failed_count": ("failure_count",),
    "failure_reason": ("message",),
    "elapsed_ms": ("elapsed",),
    "batch_count": ("batch_total", "count"),
    "datetime": ("date_time",),
}


def _canonical_key(name: str) -> str:
    """把变量名翻译成规范键；未知变量保持原样。"""
    token = str(name or "").strip()
    if not token:
        return ""
    return _ALIASES.get(token, token)


def _to_float(value: Any) -> Optional[float]:
    """尽最大努力把值转成 float，失败返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _format_value(key: str, value: Any) -> str:
    """把变量值格式化成最终文本。"""
    if key == "elapsed":
        seconds = _to_float(value)
        if seconds is not None:
            return "{0:.2f}".format(seconds)
        return "" if value is None else str(value)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (list, tuple, set, frozenset)):
        return "、".join(str(item) for item in value)
    return str(value)


def _add_derived_values(values: Dict[str, str]) -> None:
    """从已提供的安全标量推导便捷变量，不覆盖调用方显式传值。"""
    date_text = values.get("date", "").strip()
    time_text = values.get("time", "").strip()
    if date_text and time_text:
        values.setdefault("datetime", "{} {}".format(date_text, time_text))

    try:
        width = int(float(values.get("width", "")))
        height = int(float(values.get("height", "")))
    except (TypeError, ValueError):
        width = height = 0
    if width > 0 and height > 0:
        values.setdefault("pixel_count", str(width * height))
        divisor = gcd(width, height)
        values.setdefault("aspect_ratio", "{}:{}".format(width // divisor, height // divisor))
        values.setdefault("is_square", "是" if width == height else "否")

    try:
        batch_count = float(values.get("batch_count", "0"))
    except (TypeError, ValueError):
        batch_count = 0
    values.setdefault("is_batch", "是" if batch_count > 1 else "否")

    try:
        success_count = float(values.get("success_count", "0"))
    except (TypeError, ValueError):
        success_count = 0
    try:
        failed_count = float(values.get("failed_count", "0"))
    except (TypeError, ValueError):
        failed_count = 0
    values.setdefault("has_success", "是" if success_count > 0 else "否")
    values.setdefault("has_failure", "是" if failed_count > 0 else "否")


def render_template(template: str, **kwargs: Any) -> str:
    """渲染 {变量} 模板。

    - 变量名支持中英文两套写法（内置 _ALIASES 映射表），例如 elapsed 与 耗时 等价。
    - 未提供的变量原样保留，不会抛错，也不会被替换成空字符串。
    - 支持 {{ 与 }} 转义为字面量的 { 与 }。
    - 耗时（elapsed）传入数字或数字字符串时自动格式化为两位小数。

    :param template: 模板文本。
    :param kwargs: 变量值，键名可以是中文或英文。
    :return: 渲染后的文本。
    """
    if template is None:
        return ""
    text = str(template)
    if not text:
        return ""

    values: Dict[str, str] = {}
    canonical_values: Dict[str, str] = {}
    for raw_key, raw_value in kwargs.items():
        name = str(raw_key).strip()
        if not name:
            continue
        canonical = _canonical_key(name)
        formatted = _format_value(canonical, raw_value)
        if canonical and canonical not in canonical_values:
            canonical_values[canonical] = formatted
        if name not in values:
            values[name] = formatted

    _add_derived_values(canonical_values)

    # 先把转义花括号藏起来，避免被当成变量占位符
    masked = text.replace("{{", _ESCAPE_LBRACE).replace("}}", _ESCAPE_RBRACE)

    def _replace(match: "re.Match[str]") -> str:
        token = match.group(1).strip()
        if not token:
            return match.group(0)
        canonical = _canonical_key(token)
        if token in values:
            return values[token]
        if canonical and canonical in canonical_values:
            return canonical_values[canonical]
        for candidate in _ALIAS_FALLBACKS.get(canonical, ()):
            if candidate in canonical_values:
                return canonical_values[candidate]
        return match.group(0)

    rendered = _VARIABLE_RE.sub(_replace, masked)
    return rendered.replace(_ESCAPE_LBRACE, "{").replace(_ESCAPE_RBRACE, "}")


# ==================================================================== 指令解析

def _normalize_text(text: str) -> str:
    """折叠空白（含全角空格与换行）并去掉首尾空白。"""
    return _WHITESPACE_RE.sub(" ", str(text or "")).strip()


def _is_ascii_alnum(char: str) -> bool:
    return bool(char) and char.isascii() and char.isalnum()


def _should_skip_prefix(name: str, remainder: str) -> bool:
    """前缀匹配的防误判：ASCII 字母/数字直接续写不算命中（例如 img2cat）。"""
    if not name or not remainder:
        return True
    return _is_ascii_alnum(name[-1]) and _is_ascii_alnum(remainder[0])


#: 单字剩余里的「闲聊尾巴」：命中这些字则当作误触发
_CHATTER_SINGLE: Tuple[str, ...] = (
    "展", "看", "了", "的", "吗", "吧", "呢", "啊", "很", "过", "起",
    "完", "到", "成", "多", "少", "好", "真", "假", "对", "错",
)

#: 以这些虚词开头的剩余一律当误触发（如「绘画的好看」）
_VAGUE_HEADS: Tuple[str, ...] = (
    "的", "了", "吗", "吧", "呢", "啊", "怎", "什", "哪", "你", "我", "他",
)

#: 明确的内容分隔符（出现则一定算命中）
_CONTENT_SEPARATORS: Tuple[str, ...] = (" ", "\t", "　", ",", "，", ":", "：", "\n", "·", "-", "—")

#: 图片链接 / @提及 / 代码块 等结构化内容的开头
_STRUCTURED_HEAD_RE = re.compile(r"^(https?://|@|【|（)")


def _is_meaningful_content(remainder: str) -> bool:
    """判断紧接在指令后的剩余文本是否算「真正的内容」。

    拒绝：空串 / 单个闲聊字（展、看、了…）/ 以虚词开头（的、了、怎…）。
    """
    text = str(remainder or "").strip()
    if not text:
        return False
    if len(text) == 1:
        return text.lower() not in _CHATTER_SINGLE
    if text[0] in _VAGUE_HEADS:
        return False
    return True


def _strict_content_match(name: str, remainder: str) -> bool:
    """严格模式下，判断「指令 + 紧接内容」是否成立。"""
    raw = str(remainder or "")
    if not raw:
        return False
    if raw[0] in _CONTENT_SEPARATORS:
        return _is_meaningful_content(raw)
    if raw[0].isascii() and raw[0].isalnum():
        # ASCII 紧接：例如 img2cat 这种要排除；
        # 但用户写「绘画1张图」也合理，允许后面跟中文
        rest = raw[1:]
        return bool(rest) and not rest[0].isascii()
    if raw[0] in "。！？；;、":
        return False
    if _STRUCTURED_HEAD_RE.match(raw):
        return True
    return _is_meaningful_content(raw)


def parse_command(text: str, cfg: Any) -> Optional[ParsedCommand]:
    """解析用户消息，返回命中的指令；没有命中返回 None。

    匹配规则（按最长指令名优先）：

    1. 完全相等：text == 指令名；
    2. 空白分隔：text 以「指令名 + 空格」开头（换行、全角空格已折叠成空格）；
    3. 内容型指令（绘画/图片编辑）允许直接续写内容，例如
       「绘画广州塔宣传图」会被解析成 KIND_DRAW，arg = 广州塔宣传图。
    4. 菜单别名可带分组参数或连写已知分组；分组参数保留原始大小写与内部空白。
    5. 无指令命中时，仅完整分组名可直接查询；序号和唯一子串须带菜单前缀。

    :param text: 用户消息文本（调用方应已去掉唤醒前缀与 @ 部分）。
    :param cfg: PluginConfig 或任何提供 draw_commands/edit_commands/
        menu_commands/master_commands 方法的对象。
    :return: ParsedCommand 或 None。
    """
    raw = str(text or "")
    normalized = _normalize_text(raw)
    if not normalized:
        return None
    lowered = normalized.lower()
    raw_text = raw.strip()
    sections = _cfg_menu_sections(cfg)
    menu_prefix_matched = False

    # 用户配置的分组名本身也是菜单入口。精确命中时先于绘画等内容型指令，
    # 避免「绘画」等分组名被误判为绘画提示词。
    section_index = _menu_section_index(raw_text, sections, allow_partial=False)
    if section_index >= 0:
        section_name = sections[section_index]["name"]
        return ParsedCommand(
            kind=KIND_MENU_SHOW, arg=section_name, raw=raw, command=section_name
        )

    for name, kind in _command_candidates(cfg):
        if kind == KIND_MENU:
            pattern = r"\s+".join(re.escape(part) for part in name.split())
            matched = re.match(pattern, raw_text, re.IGNORECASE)
            if not matched:
                continue
            menu_prefix_matched = True
            remainder = raw_text[matched.end():]
            arg = remainder.strip()
            if not arg:
                return ParsedCommand(kind=KIND_MENU, arg="", raw=raw, command=name)
            if _menu_section_index(arg, sections) >= 0:
                return ParsedCommand(kind=KIND_MENU_SHOW, arg=arg, raw=raw, command=name)
            # 带空格的参数是明确的菜单查询，即使分组不存在也交给上层提示。
            separated = matched.end() < len(raw_text) and raw_text[matched.end()].isspace()
            if separated:
                return ParsedCommand(kind=KIND_MENU_SHOW, arg=arg, raw=raw, command=name)
            # 无空格时只接受「菜单模型」这类确实命中现有分组词的查询，
            # 避免「菜单今天真好看」「绘画菜单未知」等普通闲聊误触发。
            normalized_arg = _normalize_text(arg).lower()
            section_words = [
                _normalize_text(section.get("name", "")).lower()
                for section in sections
                if isinstance(section, dict)
            ]
            if (
                name.strip().lower() == "菜单"
                and normalized_arg
                and (
                    not section_words
                    or any(normalized_arg in section_name for section_name in section_words)
                )
            ):
                return ParsedCommand(kind=KIND_MENU_SHOW, arg=arg, raw=raw, command=name)
            continue
        name_lower = name.lower()
        if lowered == name_lower:
            return ParsedCommand(kind=kind, arg="", raw=raw, command=name)
        if lowered.startswith(name_lower + " "):
            arg = normalized[len(name):].strip()
            return ParsedCommand(kind=kind, arg=arg, raw=raw, command=name)
        if not menu_prefix_matched and kind in CONTENT_KINDS and lowered.startswith(name_lower):
            remainder = normalized[len(name):]
            if _should_skip_prefix(name, remainder):
                continue
            strict = True
            getter = getattr(cfg, "strict_trigger", None)
            if callable(getter):
                try:
                    strict = bool(getter())
                except Exception:  # noqa: BLE001 - 配置异常时保持严格
                    strict = True
            if strict and not _strict_content_match(name, remainder):
                continue
            return ParsedCommand(kind=kind, arg=remainder.strip(), raw=raw, command=name)
    section_index = _menu_section_index(raw_text, sections, allow_partial=False)
    if section_index >= 0:
        return ParsedCommand(
            kind=KIND_MENU_SHOW, arg=raw_text, raw=raw, command=sections[section_index]["name"]
        )
    return None


# ==================================================================== 菜单构建

#: 兜底菜单模板只展示状态和用户自定义分组，不注入固定功能指令。
_DEFAULT_MENU_TEMPLATE = (
    "gpt-image-2.5\n"
    "供应商：{供应商}\n"
    "协议：{协议}\n"
    "模型：{当前模型}\n"
    "尺寸：{当前尺寸}\n"
    "群状态：{群状态}\n"
    "{分组菜单}"
)

#: 只由装饰字符组成的行（空行 / 分隔线），跟随「主人指令」段落一起删除
_DECOR_LINE_RE = re.compile(r"^[\s\-—_=·*~━]+$")
#: 「主人指令」小节标题（允许 👑、【】等最多 6 个装饰前缀）
_MASTER_HEADER_RE = re.compile(r"^[\s\S]{0,6}主人[\s指令命令:：,，。【】\[\]（）()]*$")


def _template_master_line(template: str) -> int:
    """返回模板里「主人指令」占位符所在的行号；找不到返回 -1。"""
    for index, line in enumerate(str(template or "").split("\n")):
        for token in _VARIABLE_RE.findall(line):
            if _canonical_key(token) == "master_commands":
                return index
    return -1


def _strip_master_section(template: str) -> str:
    """从模板里移除「主人指令」段落，连同它上方的小节标题与分隔线。"""
    text = str(template or "")
    target = _template_master_line(text)
    if target < 0:
        return text
    lines = text.split("\n")
    start = target
    while start > 0:
        previous = lines[start - 1].strip()
        if not previous or _DECOR_LINE_RE.match(previous) or _MASTER_HEADER_RE.match(previous):
            start -= 1
            continue
        break
    return "\n".join(lines[:start] + lines[target + 1:])


def _drop_empty_section_line(text: str) -> str:
    """删除「{分组菜单}」渲染为空后残留的空行与重复分隔线。"""
    lines = str(text or "").split("\n")
    result: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped and result and not result[-1].strip():
            continue
        result.append(line)
        continue
    # 连续分隔线：保留第一条
    cleaned: List[str] = []
    for line in result:
        stripped = line.strip()
        if _DECOR_LINE_RE.match(stripped) and cleaned and _DECOR_LINE_RE.match(cleaned[-1].strip()):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def _tidy_menu_text(text: str) -> str:
    """收敛菜单排版：去掉连续空行，以及结尾孤立的空行 / 分隔线。"""
    result: List[str] = []
    for line in str(text or "").split("\n"):
        line = line.rstrip()
        if not line.strip():
            if not result or not result[-1].strip():
                continue
            result.append("")
            continue
        result.append(line)
    while result and (not result[-1].strip() or _DECOR_LINE_RE.match(result[-1].strip())):
        result.pop()
    return "\n".join(result)


def _first_or(values: Sequence[str], default: str) -> str:
    """取列表第一项，为空时返回默认值。"""
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return default


def _cfg_value(cfg: Any, method: str, *args: Any) -> Any:
    """调用 cfg 上的方法；方法缺失、不可调用或抛错时返回 None。"""
    getter = getattr(cfg, method, None)
    if not callable(getter):
        return None
    try:
        return getter(*args)
    except Exception:  # noqa: BLE001 - 配置异常不应影响菜单渲染
        return None


def _text_of(value: Any) -> str:
    """把状态值转成文本；对象优先读取 supplier / name 等属性。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    for attr in ("supplier", "supplier_name", "name", "channel_name"):
        got = getattr(value, attr, None)
        if isinstance(got, str) and got.strip():
            return got.strip()
    return ""


def _index_in(values: Sequence[Any], target: str) -> int:
    """返回 target 在 values 中的 1-based 序号；找不到返回 0。"""
    for index, value in enumerate(values or ()):
        if str(value or "").strip() == target:
            return index + 1
    return 0


def _positive_int(value: Any) -> int:
    """把状态里的序号转成 1-based 正整数；非法值返回 0。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    text = str(value or "").strip()
    if text.isdigit() and int(text) > 0:
        return int(text)
    return 0


def _as_bool_or_none(value: Any) -> Optional[bool]:
    """把状态里的开关值转成 bool；无法判断时返回 None（表示私聊场景）。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on", "已开启", "开启", "开"):
        return True
    if text in ("false", "0", "no", "off", "已关闭", "关闭", "关"):
        return False
    return None


def _format_cooldown(value: Any) -> str:
    """把冷却秒数转成菜单文案；无法读取时返回 —。"""
    seconds = _to_float(value)
    if seconds is None:
        return "—"
    if seconds <= 0:
        return "不限制"
    if float(seconds).is_integer():
        return "{0} 秒".format(int(seconds))
    return "{0:g} 秒".format(seconds)


def render_numbered_list(
    title: str,
    items: Sequence[str],
    current: str = "",
    page: int = 1,
    page_size: int = 20,
) -> str:
    """渲染带序号的列表（序号从 1 开始，跨页连续编号）。

    - 命中 current 的条目追加「当前」标记；
    - page / page_size 非法或越界时自动收敛到合法范围；
    - items 为空（或全是空白）时返回 title + "（暂无）"；
    - 条目多于 page_size 时，末尾附加「第 X/Y 页 · 发送 指令 页码 翻页」提示。

    :param title: 列表标题，同时作为翻页提示里的指令名。
    :param items: 条目文本列表。
    :param current: 当前生效的条目（加 ✅ 标记）。
    :param page: 页码，从 1 开始。
    :param page_size: 每页条数，非法值按 20 处理。
    :return: 渲染后的多行文本。
    """
    head = str(title or "").strip()
    values = [str(item or "").strip() for item in (items or ())]
    values = [item for item in values if item]
    if not values:
        return head + "（暂无）"

    try:
        size = int(page_size)
    except (TypeError, ValueError):
        size = 20
    if size <= 0:
        size = len(values)
    total_pages = (len(values) + size - 1) // size

    try:
        current_page = int(page)
    except (TypeError, ValueError):
        current_page = 1
    if current_page < 1:
        current_page = 1
    if current_page > total_pages:
        current_page = total_pages

    start = (current_page - 1) * size
    target = str(current or "").strip()
    lines = [head] if head else []
    for offset, item in enumerate(values[start:start + size], start=start):
        mark = " [当前]" if target and item.lower() == target.lower() else ""
        lines.append("{0}. {1}{2}".format(offset + 1, item, mark))
    if total_pages > 1:
        hint_page = current_page + 1 if current_page < total_pages else current_page - 1
        lines.append(
            "第 {0}/{1} 页 · 发送 {2} {3} 翻页".format(current_page, total_pages, head, hint_page)
        )
    return "\n".join(lines)


#: 单个分组最多展示的指令条数（菜单里过长会很难看）
_MENU_SECTION_SHOW_LIMIT = 8


def _cfg_menu_sections(cfg: Any) -> List[dict]:
    """读取菜单分组；未配置时返回可用的出厂分组。"""
    getter = getattr(cfg, "menu_sections", None)
    if not callable(getter):
        return [dict(item, items=list(item["items"])) for item in _DEFAULT_MENU_SECTIONS]
    try:
        value = getter()
    except Exception:  # noqa: BLE001
        return [dict(item, items=list(item["items"])) for item in _DEFAULT_MENU_SECTIONS]
    result: List[dict] = []
    if not isinstance(value, (list, tuple)):
        return [dict(item, items=list(item["items"])) for item in _DEFAULT_MENU_SECTIONS]
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        items = [str(entry or "").strip() for entry in (item.get("items") or ())]
        items = [entry for entry in items if entry]
        if name:
            result.append({"name": name, "items": items})
    if result or value == []:
        return result
    return [dict(item, items=list(item["items"])) for item in _DEFAULT_MENU_SECTIONS]


def render_menu_sections(cfg: Any, *, compact: bool = False) -> str:
    """把自定义菜单分组渲染成多行文本（供 {分组菜单} 变量与「菜单列表」使用）。

    :param cfg: PluginConfig 或同类对象。
    :param compact: True 时只输出「分组名：指令、指令」单行形式，
        适合塞进主菜单；False 时输出带缩进的完整形式。
    """
    sections = _cfg_menu_sections(cfg)
    if not sections:
        return ""
    lines: List[str] = []
    for section in sections:
        name = section["name"]
        items = section["items"]
        if compact:
            body = "　".join(items[:_MENU_SECTION_SHOW_LIMIT]) or "（空）"
            lines.append("{0}：{1}".format(name, body))
            continue
        lines.append(name)
        if not items:
            lines.append("　　（空，可用「设置菜单 {0} 指令A|指令B」补充）".format(name))
            continue
        for item in items[:_MENU_SECTION_SHOW_LIMIT]:
            lines.append("　　{0}".format(item))
        if len(items) > _MENU_SECTION_SHOW_LIMIT:
            lines.append("　　… 还有 {0} 项".format(len(items) - _MENU_SECTION_SHOW_LIMIT))
    return _strip_menu_emoji("\n".join(lines))


_EMOJI_RE = re.compile(
    "[\\U0001F1E6-\\U0001F1FF\\U0001F300-\\U0001FAFF\\u2600-\\u27BF\\uFE0F\\u200D]"
)


def _strip_menu_emoji(text: str) -> str:
    """移除菜单展示中的 emoji；用户自定义指令本身仍原样保存。"""
    return _EMOJI_RE.sub("", str(text or ""))


def _menu_section_index(text: str, sections: Sequence[dict], *, allow_partial: bool = True) -> int:
    """按原始全名、规范化全名、序号或唯一子串定位分组；冲突时拒绝猜测。"""
    target = str(text or "").strip()
    if not target:
        return -1
    for index, section in enumerate(sections):
        if section["name"] == target:
            return index
    lowered = _normalize_text(target).lower()
    names = [_normalize_text(section["name"]).lower() for section in sections]
    hits = [index for index, name in enumerate(names) if name == lowered]
    if hits:
        return hits[0] if len(hits) == 1 else -1
    if not allow_partial:
        return -1
    if target.isdecimal():
        try:
            index = int(target) - 1
        except ValueError:
            return -1
        return index if 0 <= index < len(sections) else -1
    hits = [index for index, name in enumerate(names) if lowered in name]
    return hits[0] if len(hits) == 1 else -1


def build_section_menu(cfg: Any, name: str) -> Optional[str]:
    """渲染单个菜单分组的详情；分组不存在返回 None。"""
    target = str(name or "").strip()
    if not target:
        return None
    sections = _cfg_menu_sections(cfg)
    if not sections:
        return None
    index = _menu_section_index(target, sections)
    if index < 0:
        return None
    section = sections[index]
    lines = ["{0}（{1}/{2}）".format(section["name"], index + 1, len(sections))]
    lines.append("━━━━━━━━━━━━━━━")
    if section["items"]:
        for position, item in enumerate(section["items"], start=1):
            lines.append("{0}. {1}".format(position, item))
    else:
        lines.append("（该分组还没有指令）")
    lines.append("━━━━━━━━━━━━━━━")
    lines.append("主人可用「设置菜单 {0} 指令A|指令B」修改本分组".format(section["name"]))
    return _strip_menu_emoji("\n".join(lines))


def resolve_choice_by_name(text: str, options: Sequence[str]) -> Tuple[int, str]:
    """按 序号 / 完全匹配 / 唯一子串 解析选项（大小写不敏感）。"""
    values = [str(item) for item in (options or ())]
    raw = str(text or "").strip()
    if not raw or not values:
        return (-1, "")
    if raw.isdigit():
        position = int(raw) - 1
        if 0 <= position < len(values):
            return (position, values[position])
        return (-1, "")
    lowered = raw.lower()
    for position, value in enumerate(values):
        if value.strip().lower() == lowered:
            return (position, value)
    hits = [position for position, value in enumerate(values) if lowered in value.lower()]
    if len(hits) == 1:
        return (hits[0], values[hits[0]])
    hits = [position for position, value in enumerate(values) if value.lower() in lowered]
    if len(hits) == 1:
        return (hits[0], values[hits[0]])
    return (-1, "")


def build_menu(cfg: Any, state_view: Optional[dict] = None, *, is_master: bool = True) -> str:
    """构建菜单文本。

    优先使用 cfg.menu_text() 作为模板渲染，渲染结果为空时回退到内置默认菜单。
    is_master=False 时会移除「主人指令」整段（含其上方的小节标题与分隔线）。
    返回值不超过 MENU_MAX_LENGTH 个字符，超出会截断并加省略号。
    仅从 state_view 透传公共上下文的标量值；请求结果与运行时对象不进入菜单模板。

    :param cfg: PluginConfig 或同类对象。
    :param state_view: 一般来自 core/state.py 的 resolved()，形如
        {"supplier":..., "supplier_index":..., "protocol":..., "protocol_index":...,
        "model":..., "model_index":..., "size":..., "group_enabled":..., "overrides":{...}}。
    :param is_master: 是否拥有主人权限，False 时不输出「主人指令」段落。
    :return: 菜单文本。
    """
    view = dict(state_view or {})

    draw_commands = _cfg_list(cfg, "draw_commands", _FALLBACK_DRAW_COMMANDS)
    edit_commands = _cfg_list(cfg, "edit_commands", _FALLBACK_EDIT_COMMANDS)
    menu_commands = _cfg_list(cfg, "menu_commands", _FALLBACK_MENU_COMMANDS)
    master_commands = _cfg_list(cfg, "master_commands", _FALLBACK_MASTER_COMMANDS)
    supplier_commands = _cfg_list(cfg, "supplier_commands", _FALLBACK_SUPPLIER_COMMANDS)

    def _supplier_command(index: int, fallback: str) -> str:
        """按契约顺序取 supplier_commands 里的第 index 项，缺失时用内置名。"""
        if 0 <= index < len(supplier_commands):
            name = str(supplier_commands[index] or "").strip()
            if name:
                return name
        return fallback

    protocol_key = (
        _text_of(view.get("protocol_key"))
        or _text_of(view.get("protocol"))
        or _text_of(_cfg_value(cfg, "active_protocol"))
    )
    protocol_key = str(protocol_key or "").strip()
    protocol_text = _text_of(view.get("protocol_label"))
    if not protocol_text:
        protocol_text = PROTOCOL_SHORT_LABELS.get(protocol_key, protocol_key) or "—"

    supplier_names = _cfg_list(cfg, "supplier_names", ())
    protocols = [str(item) for item in (_cfg_value(cfg, "protocols") or ())]
    models = [str(item) for item in (_cfg_value(cfg, "models_for", protocol_key) or ())]

    supplier_index = _positive_int(view.get("supplier_index"))
    supplier = _text_of(view.get("supplier")) or _text_of(view.get("supplier_name"))
    if not supplier:
        supplier = _text_of(view.get("provider")) or _text_of(view.get("channel"))
    if not supplier:
        supplier = _text_of(view.get("channel_name"))
    if not supplier and supplier_index:
        supplier = str(supplier_names[supplier_index - 1]) if supplier_index <= len(supplier_names) else ""
    if not supplier:
        supplier = _cfg_text(cfg, "active_supplier_name", "")
    if not supplier:
        supplier = _cfg_text(cfg, "active_channel_name", "")
    supplier = supplier.strip() or "—"
    if not supplier_index and supplier != "—":
        supplier_index = _index_in(supplier_names, supplier)

    protocol_index = _positive_int(view.get("protocol_index"))
    if not protocol_index and protocol_key:
        protocol_index = _index_in(protocols, protocol_key)

    model = _text_of(view.get("model")) or _text_of(_cfg_value(cfg, "model_for")) or "—"
    size = _text_of(view.get("size")) or _text_of(_cfg_value(cfg, "active_size")) or "—"

    model_index = _positive_int(view.get("model_index"))
    if not model_index and model != "—":
        model_index = _index_in(models, model)

    group_flag = _as_bool_or_none(view.get("group_enabled"))
    if group_flag is True:
        group_status = "已开启"
    elif group_flag is False:
        group_status = "已关闭"
    else:
        group_status = "私聊"

    trigger_key = _text_of(view.get("trigger_mode")) or _text_of(_cfg_value(cfg, "trigger_mode"))
    trigger_key = trigger_key.lower()
    if not trigger_key:
        trigger_text = "—"
    elif trigger_key == "command":
        trigger_text = "免@"
    else:
        trigger_text = "需@"

    cooldown_value = view.get("cooldown")
    if cooldown_value is None:
        cooldown_value = _cfg_value(cfg, "cooldown")
    cooldown_text = _format_cooldown(cooldown_value)

    kwargs = {
        "model": model,
        "current_model": model,
        "size": size,
        "current_size": size,
        "provider": supplier,
        "supplier": supplier,
        "channel": supplier,
        "supplier_index": supplier_index or "—",
        "protocol_key": protocol_key or "—",
        "protocol": protocol_text,
        "protocol_label": protocol_text,
        "protocol_index": protocol_index or "—",
        "model_index": model_index or "—",
        "trigger_mode": trigger_text,
        "group_status": group_status,
        "cooldown": cooldown_text,
        "timeout": _cfg_text(cfg, "timeout", ""),
        "draw_command": _first_or(draw_commands, "绘画"),
        "edit_command": _first_or(edit_commands, "图片编辑"),
        "menu_command": _first_or(menu_commands, "菜单"),
        "supplier_menu": _supplier_command(0, "供应商列表"),
        "model_menu": _supplier_command(4, "模型列表"),
        "size_menu": "尺寸列表",
        "protocol_menu": _supplier_command(2, "协议列表"),
        "group_menu": "群列表",
        "help_command": "帮助",
        "stats_command": _supplier_command(6, "运行统计"),
        "reset_command": _supplier_command(7, "重置设置"),
        "master_commands": "、".join(master_commands),
        "draw_commands": "、".join(draw_commands),
        "edit_commands": "、".join(edit_commands),
        "menu_commands": "、".join(menu_commands),
        "menu_sections": render_menu_sections(cfg, compact=True),
        "menu_list": _first_or(_cfg_list(cfg, "menu_commands", _FALLBACK_MENU_COMMANDS), "菜单"),
    }
    context_keys = {
        "user", "user_id", "group_id", "group_name", "group_mode", "platform",
        "bot_instance", "raw_message", "message_id", "session_id", "time", "date",
        "width", "height",
    }
    for key, value in view.items():
        if _canonical_key(key) in context_keys and (
            value is None or isinstance(value, (str, int, float, bool))
        ):
            kwargs[key] = value
    timeout_getter = getattr(cfg, "timeout", None)
    if callable(timeout_getter):
        try:
            kwargs["timeout"] = timeout_getter()
        except Exception:  # noqa: BLE001 - 读取失败时保持空值
            kwargs["timeout"] = ""

    template = _cfg_text(cfg, "menu_text", "")
    if not is_master:
        template = _strip_master_section(template)
    rendered = render_template(template, **kwargs)
    if not rendered.strip():
        fallback = _DEFAULT_MENU_TEMPLATE
        if not is_master:
            fallback = _strip_master_section(fallback)
        rendered = render_template(fallback, **kwargs)
    rendered = _strip_menu_emoji(_drop_empty_section_line(rendered)).strip()
    if not is_master:
        rendered = _tidy_menu_text(rendered)
    if len(rendered) > MENU_MAX_LENGTH:
        rendered = rendered[: MENU_MAX_LENGTH - 1].rstrip() + "…"
    return rendered


__all__ = [
    "ALL_KINDS",
    "CONTENT_KINDS",
    "QUERY_KINDS",
    "KIND_ADD_SIZE",
    "KIND_DEL_SIZE",
    "KIND_DRAW",
    "KIND_EDIT",
    "KIND_GROUP_OFF",
    "KIND_GROUP_ON",
    "KIND_GROUPS",
    "KIND_HELP",
    "KIND_MASTER_ADD",
    "KIND_MASTER_DEL",
    "KIND_MENU",
    "KIND_MENU_SHOW",
    "KIND_MODELS",
    "KIND_PROTOCOLS",
    "KIND_RELOAD",
    "KIND_RESET",
    "KIND_SIZES",
    "KIND_STATS",
    "KIND_SUPPLIERS",
    "KIND_SWITCH_MODEL",
    "KIND_SWITCH_PROTOCOL",
    "KIND_SWITCH_SIZE",
    "KIND_SWITCH_SUPPLIER",
    "KIND_WHOAMI",
    "MASTER_KINDS",
    "MENU_MAX_LENGTH",
    "ParsedCommand",
    "all_command_names",
    "build_menu",
    "build_section_menu",
    "render_menu_sections",
    "resolve_choice_by_name",
    "is_master_kind",
    "parse_command",
    "strict_trigger_allows",
    "render_numbered_list",
    "render_template",
]


def strict_trigger_allows(name: str, remainder: str) -> bool:
    """严格模式下「指令 + 紧接内容」是否允许触发（供外部复用）。"""
    return _strict_content_match(name, remainder)
