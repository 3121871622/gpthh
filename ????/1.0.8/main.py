"""gpt-image-2.5绘画 · 插件主入口。

本模块只负责「事件接入」：把 AstrBot 的消息事件解析成指令，调用 core/ 下的
协议、图像与配置模块，并把结果发回会话。业务逻辑全部在 core/ 中，便于单测。

支持平台：OneBot v11（aiocqhttp）、QQ 官方机器人（qq_official / webhook）。
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter

from .core.commands import (
    KIND_ADD_SIZE,
    KIND_GACHA,
    KIND_IMG2PROMPT,
    KIND_MENU_DEL,
    KIND_MENU_LIST,
    KIND_MENU_SET,
    KIND_MENU_SHOW,
    KIND_PRESET_ADD,
    KIND_PRESET_DEL,
    KIND_PRESET_LIST,
    KIND_PROMPT_TOOLS,
    KIND_RANK,
    KIND_REPEAT,
    KIND_DEL_SIZE,
    KIND_DRAW,
    KIND_EDIT,
    KIND_GROUP_OFF,
    KIND_GROUP_ON,
    KIND_GROUPS,
    KIND_HELP,
    KIND_MASTER_ADD,
    KIND_MASTER_DEL,
    KIND_MENU,
    KIND_MODELS,
    KIND_PROTOCOLS,
    KIND_RELOAD,
    KIND_RESET,
    KIND_SIZES,
    KIND_STATS,
    KIND_SUPPLIERS,
    KIND_SWITCH_MODEL,
    KIND_SWITCH_PROTOCOL,
    KIND_SWITCH_SUPPLIER,
    KIND_SWITCH_SIZE,
    KIND_WHOAMI,
    ParsedCommand,
    build_menu,
    build_section_menu,
    is_master_kind,
    parse_command,
    render_menu_sections,
    render_numbered_list,
    render_template,
    resolve_choice_by_name,
)
from .core import config as _config_module
from .core.config import PluginConfig
from .core.images import collect_input_images, extract_image_urls, send_result
from .core.models import (
    PROTOCOL_SHORT_LABELS,
    PROTOCOL_ORDER,
    GenerateRequest,
    ProtocolError,
    default_model_for,
    extract_size_hints,
    looks_like_size,
    normalize_size,
    protocol_supports_size,
    resolve_choice,
    strip_size_hints,
)
from .core.protocols import create_protocol, protocol_label
from .core.assist import build_structured_prompt, image_to_prompt, translate_prompt
from .core.fun import FunStats
from .core.presets import (
    pick_flavor_line,
    pick_gacha_style,
    render_preset,
)
from .core.state import SessionState
from .core.stats import PluginStats
from .core.webapi import register_web_apis

PLUGIN_VERSION = "1.0.8"

#: 不受「群开关 / 私聊开关」限制的指令类型（管理类，仍需主人权限）
_SCOPE_EXEMPT_KINDS = frozenset(
    {
        KIND_GROUPS,
        KIND_GROUP_ON,
        KIND_GROUP_OFF,
        KIND_MASTER_ADD,
        KIND_MASTER_DEL,
        KIND_RELOAD,
        KIND_WHOAMI,
    }
)

#: 需要主人权限的指令类型（除 commands.MASTER_KINDS 外的额外保护）
_EXTRA_MASTER_KINDS = frozenset({KIND_RESET})

#: 允许在「会话状态」中使用覆盖值的字段（用于「重置设置」展示）
_OVERRIDE_LABELS = {
    "supplier": "供应商",
    "protocol": "协议",
    "model": "模型",
    "size": "尺寸",
}

#: 冷却提示文案模板
_COOLDOWN_HINT = "⏳ 请稍等，冷却中还需 {seconds} 秒才能再次使用绘画 / 图片编辑。"

#: 本群已关闭时的反馈（避免用户以为机器人失灵）
_GROUP_OFF_HINT = (
    "⛔ 本群的绘画功能已关闭。\n"
    "插件主人可在本群发送「开群」重新开启。"
)

#: 同一群的「已关闭」提示最小间隔（秒）
_GROUP_OFF_HINT_INTERVAL = 60.0

#: 同一条消息的处理去重窗口（秒）。
#: Webhook 侧框架只按平台事件 id 去重，WS 侧完全不去重；
#: 群聊 @ 与非 @ 又是两条独立通道，平台异常重推时会重复调用绘画接口（重复扣费）。
_MESSAGE_DEDUP_WINDOW = 90.0

#: 去重字典的硬上限（防止群刷屏时内存无限增长）
_MESSAGE_DEDUP_MAX = 512

#: QQ 官方机器人平台名（websocket / webhook 两种协议）
#: 官方适配器在群聊里「非 @ 消息」同样会投递事件（is_at_or_wake_command 恒为 False），
#: 因此这两个平台需要单独放行，否则用户必须每次都 @ 机器人才能用指令。
_QQ_OFFICIAL_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})


def _is_qq_official(event: Any) -> bool:
    """判断事件是否来自 QQ 官方机器人（websocket / webhook）。"""
    getter = getattr(event, "get_platform_name", None)
    if not callable(getter):
        return False
    try:
        name = str(getter() or "").strip().lower()
    except Exception:  # noqa: BLE001 - 适配器实现差异
        return False
    if name in _QQ_OFFICIAL_PLATFORMS:
        return True
    # 容错：部分平台名带前缀 / 后缀（例如 qq_official_sandbox）
    return name.startswith("qq_official")


#: 形如「@昵称(123456)」的被 @ 用户占位，编辑提示词时需要剔除
_MENTION_RE = re.compile(r"@[^\s@()]{0,64}\(\d{1,20}\)")
#: 连续半角/全角空格压缩（保留换行，避免破坏用户的排版提示词）
_SPACE_RE = re.compile(r"[ \t\u3000]{2,}")

_GROUP_MODE_LABELS = {
    "all": "所有群聊生效",
    "whitelist": "仅白名单群生效",
    "blacklist": "黑名单群禁用",
}

_REQUIRED_CHANNEL_HINT = (
    "⚠️ 还没有可用的绘画通道。\n"
    "请在 AstrBot 后台「插件 → gpt-image-2.5绘画 → 设置」中填写中转站地址与密钥。"
)

_SETTINGS_PATH = "AstrBot 后台「插件 → gpt-image-2.5绘画 → 设置」"


def _channel_ready(channel: Any) -> tuple:
    """检查通道是否可用，返回 (是否可用, 不可用原因)。

    只拦截「缺少接口地址」这种必然失败的情况；``api_key`` 为空时仍会尝试请求
    （部分本地代理无需密钥），失败后由调用方给出针对性提示。
    """
    if channel is None:
        return False, _REQUIRED_CHANNEL_HINT
    if not _text_of(getattr(channel, "base_url", "")).strip():
        return False, (
            "⚠️ 当前通道「%s」还没有填写接口地址。\n请在 %s 中补全 Base URL。"
            % (_text_of(getattr(channel, "name", "")) or "(未命名)", _SETTINGS_PATH)
        )
    return True, ""


def _auth_hint(channel: Any, error: Any) -> str:
    """接口返回 401/403 时补充排查建议。"""
    status = getattr(error, "status", None)
    if status not in (401, 403):
        return ""
    if _text_of(getattr(channel, "api_key", "")).strip():
        return "\n💡 密钥可能无效或已过期，请在后台重新填写。"
    return "\n💡 当前通道没有填写 API Key，请在 %s 中补全。" % _SETTINGS_PATH


def _text_of(value: Any, default: str = "") -> str:
    """安全转字符串。"""
    if value is None:
        return default
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return default


def _safe_number(value: Any, default: float = 0) -> float:
    """把模板统计值安全转换为数字，避免自定义文案影响主流程。"""
    if isinstance(value, bool):
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


#: 「全局切换」关键字：切换类指令带上它时写入配置（所有人、重启后仍生效）
_GLOBAL_KEYWORDS = ("全局", "所有人", "永久", "global")


def _split_global(arg: str) -> tuple:
    """拆分「全局」标记，返回 (去掉标记的正文, 是否全局)。

    只把**末尾**出现的单个关键字当作标记；正文里包含同名文字不受影响。
    """
    text = _text_of(arg).strip()
    lowered = text.lower()
    for keyword in _GLOBAL_KEYWORDS:
        if lowered.endswith(keyword):
            head = text[: len(text) - len(keyword)].strip(" \t,，、")
            if head:
                return head, True
            if lowered == keyword:
                # 正文里只有「全局」两个字：按「缺参数」处理，
                # 交给调用方给出用法提示，避免把它当成模型/尺寸名。
                return "", True
    return text, False


def _clean_prompt(text: str) -> str:
    """清理提示词：去掉被 @ 用户占位、压缩多余空格，但保留换行。"""
    cleaned = _MENTION_RE.sub(" ", _text_of(text))
    cleaned = _SPACE_RE.sub(" ", cleaned)
    return cleaned.strip()


#: 可重试的错误关键字（超时 / 网络抖动 / 服务端 5xx / 限流）
_RETRY_KEYWORDS = (
    "timeout",
    "timed out",
    "超时",
    "connect",
    "connection",
    "reset",
    "disconnect",
    "payload",
    "incomplete",
    "连接",
    "网络",
    "temporarily",
    "overloaded",
    "rate limit",
    "too many requests",
)
#: 明确不可重试的错误关键字（额度 / 鉴权 / 参数类，重试也没有意义）
_NO_RETRY_KEYWORDS = (
    "余额",
    "额度",
    "欠费",
    "insufficient",
    "quota",
    "unauthorized",
    "invalid api key",
    "forbidden",
    "not found",
    "不存在",
    "无法解析",
)


def _should_retry(error: Any) -> bool:
    """判断一次失败是否值得重试（超时 / 网络 / 5xx / 429）。"""
    if isinstance(error, asyncio.TimeoutError):
        return True
    status = getattr(error, "status", None)
    if isinstance(status, int):
        if status == 429 or status >= 500:
            return True
        if 400 <= status < 500:
            # 4xx（除 429 限流外）属于请求本身的问题，重试不会变好
            return False
    message = ("%s %s" % (type(error).__name__, error)).lower()
    if any(keyword in message for keyword in _NO_RETRY_KEYWORDS):
        return False
    return any(keyword in message for keyword in _RETRY_KEYWORDS)

class _DrawingCommandFilter(filter.CustomFilter):
    """只在「文本命中绘画相关指令」时放行，避免插件监听全部消息。

    AstrBot 的唤醒机制会因 filter 通过而判定为「被唤醒」，因此这里必须精确
    判断：只有解析出指令（且满足 @ / 唤醒前缀 / 私聊条件）才返回 True。
    """

    plugin: "Main | None" = None
    raise_error = False

    def filter(self, event: AstrMessageEvent, cfg: Any) -> bool:
        plugin = _DrawingCommandFilter.plugin
        if plugin is None:
            return False
        try:
            text = event.get_message_str()
        except Exception:  # noqa: BLE001
            return False
        if plugin._menu_command_override(text) is None and not parse_command(text, plugin.cfg):
            return False
        if plugin._is_duplicate_event(event):
            return False
        # 触发方式一：command 模式 —— 只要「消息以指令开头」就响应，
        # 不需要 @ 机器人，也不需要唤醒前缀。
        try:
            if plugin.cfg.trigger_mode() == "command":
                return True
        except Exception:  # noqa: BLE001 - 配置读取失败时退回默认行为
            pass
        # 触发方式二：QQ 官方机器人 —— 群聊免 @。
        # 官方适配器本来就会把非 @ 的群消息投递上来（is_at_or_wake_command 恒为
        # False），若仍按 at 模式拦截，用户会以为「必须 @ 才有反应」。
        try:
            if _is_qq_official(event) and not event.is_private_chat():
                return True
        except Exception:  # noqa: BLE001
            pass
        # 触发方式三（默认）：群聊需要 @ 机器人 或使用唤醒前缀；私聊直接生效
        try:
            if event.is_at_or_wake_command:
                return True
            return bool(event.is_private_chat())
        except Exception:  # noqa: BLE001
            return False


class Main(star.Star):
    """gpt-image-2.5绘画插件。"""

    def __init__(self, context: star.Context, config: Any = None) -> None:
        super().__init__(context)
        self.cfg = PluginConfig(config)
        self.state = SessionState(self)
        #: 运行统计（成功 / 失败 / 平均耗时），供「运行统计」指令与设置页使用
        self.stats = PluginStats(self._resolve_data_dir())
        #: 抽卡 / 排行榜统计（需求 8 / 9），落盘在 data/plugin_data 下
        self.fun = FunStats(self._resolve_data_dir())
        #: 最近一次出图请求（供「重画 / 再来一张」复用），按会话保存
        self._last_requests: dict = {}
        self._sem: Any = None
        self._sem_loop: Any = None
        self._sem_limit = 0
        #: 同一用户的冷却时间戳（unified_msg_origin -> 上次开始请求的时间）
        self._cooldowns: dict = {}
        #: 已处理过的消息（平台实例 + 会话 + message_id -> monotonic）
        self._seen_messages: dict = {}
        #: 群已关闭提示的最后时间（群号 -> monotonic），用于限流
        self._group_off_hinted: dict = {}
        #: 最近交互过的用户 / 群（供设置页「一键获取」使用），最新在前
        self.recent_users: list = []
        self.recent_groups: list = []

    # --------------------------------------------------------------- 生命周期
    async def initialize(self) -> None:
        _DrawingCommandFilter.plugin = self
        # 旧配置（providers / active_provider）自动迁移为供应商模型
        try:
            if self.cfg.migrate():
                self.cfg.save()
                logger.info("[gpt-image] 已把旧版「通道」配置迁移为「供应商」")
        except Exception as exc:  # noqa: BLE001 - 迁移失败不影响启动
            logger.warning(f"[gpt-image] 旧配置迁移失败：{exc}")
        try:
            await self.state.load()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[gpt-image] 加载会话状态失败：{exc}")
        try:
            register_web_apis(self)
            logger.info("[gpt-image] 设置页面与 Web API 注册完成")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[gpt-image] 注册 Web API 失败：{exc}")
        logger.info(
            "[gpt-image] v%s 已加载，当前供应商：%s · 协议：%s"
            % (
                PLUGIN_VERSION,
                self.cfg.active_supplier_name() or "(未配置)",
                self.cfg.active_protocol(),
            )
        )

    async def terminate(self) -> None:
        _DrawingCommandFilter.plugin = None

    # ------------------------------------------------------------ 数据目录
    @staticmethod
    def _resolve_data_dir() -> str:
        """返回插件数据目录（优先框架标准路径，失败时退回当前工作目录）。"""
        try:
            resolve = getattr(_config_module, "resolve_data_dir", None)
            if callable(resolve):
                return str(resolve())
        except Exception:  # noqa: BLE001
            pass
        return "data/plugin_data/astrbot_plugin_gpt_image"

    # ------------------------------------------------------------ 并发 / 渲染
    def _acquire(self) -> Any:
        limit = max(1, self.cfg.max_concurrent())
        loop = asyncio.get_running_loop()
        if self._sem is None or self._sem_loop is not loop or self._sem_limit != limit:
            self._sem = asyncio.Semaphore(limit)
            self._sem_loop = loop
            self._sem_limit = limit
        return self._sem

    def _template_vars(self, event: AstrMessageEvent, info: dict, **extra: Any) -> dict:
        channel = info.get("channel")
        protocol_key = _text_of(info.get("protocol"))
        supplier = _text_of(info.get("supplier") or info.get("channel_name"))
        now = time.localtime()

        def _event_value(method: str, fallback: str = "") -> str:
            getter = getattr(event, method, None)
            if not callable(getter):
                return fallback
            try:
                return _text_of(getter()) or fallback
            except Exception:
                return fallback

        platform = _event_value("get_platform_name")
        bot_instance = _event_value("get_platform_id")
        raw_message = _event_value("get_message_str")
        group_id = _event_value("get_group_id")
        group_name = _event_value("get_group_name")
        user_name = _event_value("get_sender_name")
        session_id = _text_of(getattr(event, "unified_msg_origin", ""))
        message_id = _text_of(getattr(getattr(event, "message_obj", None), "message_id", ""))
        group_enabled = info.get("group_enabled")
        if group_enabled is None and group_id:
            try:
                group_enabled = self.cfg.is_group_allowed(group_id)
            except Exception:  # noqa: BLE001
                group_enabled = False
        group_status = "已开启" if group_id and group_enabled else (
            "已关闭" if group_id else "私聊"
        )
        is_group = bool(group_id)
        is_private = not is_group
        request_time = time.strftime("%Y-%m-%d %H:%M:%S", now)
        weekday = time.strftime("%A", now)
        base_url = _text_of(getattr(channel, "base_url", ""))
        variables = {
            "model": _text_of(info.get("model")),
            "size": _text_of(info.get("size")),
            "provider": supplier,
            "supplier": supplier,
            "协议": PROTOCOL_SHORT_LABELS.get(protocol_key, protocol_key or "—"),
            "protocol": protocol_key,
            "protocol_key": protocol_key,
            "protocol_label": PROTOCOL_SHORT_LABELS.get(protocol_key, protocol_key or "—"),
            "timeout": self.cfg.timeout(),
            "通道地址": base_url,
            "接口地址": base_url,
            "base_url": base_url,
            "user": user_name,
            "user_name": user_name,
            "nickname": user_name,
            "user_id": _event_value("get_sender_id"),
            "time": time.strftime("%H:%M:%S", now),
            "date": time.strftime("%Y-%m-%d", now),
            "request_time": request_time,
            "year": now.tm_year,
            "month": now.tm_mon,
            "day": now.tm_mday,
            "weekday": weekday,
            "group_id": group_id,
            "group_name": group_name,
            "group_status": group_status,
            "group_enabled": bool(group_enabled) if group_id else False,
            "is_group": is_group,
            "is_private": is_private,
            "group_mode": self.cfg.group_mode(),
            "platform": platform,
            "bot_instance": bot_instance,
            "raw_message": raw_message,
            "message": raw_message,
            "message_id": message_id,
            "session_id": session_id,
            "session": session_id,
            "conversation_id": session_id,
            "supplier_name": supplier,
            "channel_name": supplier,
            "model_name": _text_of(info.get("model")),
            "current_model": _text_of(info.get("model")),
            "current_size": _text_of(info.get("size")),
            "supplier_index": info.get("supplier_index", 0),
            "supplier_total": info.get("supplier_total", 0),
            "protocol_index": info.get("protocol_index", 0),
            "model_index": info.get("model_index", 0),
            "model_total": info.get("model_total", 0),
            "plugin_version": PLUGIN_VERSION,
        }
        variables.update(extra)
        dimensions = re.fullmatch(r"(\d+)[x×](\d+)", _text_of(variables.get("size")), re.IGNORECASE)
        variables["width"] = dimensions.group(1) if dimensions else ""
        variables["height"] = dimensions.group(2) if dimensions else ""
        prompt_value = _text_of(variables.get("prompt") or variables.get("original_prompt"))
        variables.setdefault("prompt_length", len(prompt_value))
        variables.setdefault("has_images", bool(variables.get("input_count")))
        success_count = _safe_number(variables.get("success_count"), 0)
        failed_count = _safe_number(variables.get("failed_count"), 0)
        total_attempts = success_count + failed_count
        variables.setdefault(
            "success_rate",
            round(success_count * 100 / total_attempts, 2) if total_attempts else 0,
        )
        variables.setdefault("remaining_count", max(0, _safe_number(variables.get("batch_total"), 0) - _safe_number(variables.get("batch_index"), 0)))
        variables.setdefault("attempts_allowed", 1 + self._retry_times())
        variables.setdefault("retry_limit", self._retry_times())
        return variables

    @staticmethod
    def _remember(bucket: list, entry: dict, limit: int = 30) -> None:
        """把一条交互记录放到最前面并去重（同 ID 只保留最新一条）。"""
        identifier = entry.get("id")
        if not identifier:
            return
        bucket[:] = [item for item in bucket if item.get("id") != identifier]
        bucket.insert(0, entry)
        del bucket[limit:]

    def _record_context(self, event: AstrMessageEvent) -> None:
        """记录本次交互的用户与群，方便在设置页一键获取。"""
        try:
            platform = _text_of(event.get_platform_name())
            platform_id = _text_of(event.get_platform_id())
            sender_id = _text_of(event.get_sender_id())
            sender_name = _text_of(event.get_sender_name())
            group_id = _text_of(event.get_group_id())
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:  # noqa: BLE001
            return

        if sender_id:
            self._remember(
                self.recent_users,
                {
                    "id": sender_id,
                    "name": sender_name,
                    "platform": platform,
                    "platform_id": platform_id,
                    "time": stamp,
                },
            )
        if group_id:
            group_name = ""
            getter = getattr(event, "get_group_name", None)
            if callable(getter):
                try:
                    group_name = _text_of(getter())
                except Exception:  # noqa: BLE001 - 部分适配器没有群名
                    group_name = ""
            self._remember(
                self.recent_groups,
                {
                    "id": group_id,
                    "name": group_name,
                    "platform": platform,
                    "platform_id": platform_id,
                    "time": stamp,
                },
            )

    @staticmethod
    async def _reply(event: AstrMessageEvent, text: str) -> None:
        try:
            await event.send(event.plain_result(text))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[gpt-image] 发送文本失败：{exc}")

    # ---------------------------------------------------------------- 事件入口
    @filter.custom_filter(_DrawingCommandFilter)
    async def on_message(self, event: AstrMessageEvent) -> None:
        """统一处理绘画 / 编辑 / 菜单 / 切换类指令。"""
        try:
            await self._handle(event)
        except Exception as exc:  # noqa: BLE001 - 兜底，避免整条管道报错
            logger.exception(f"[gpt-image] 处理指令失败：{exc}")
            await self._reply(event, f"😵 处理指令时出错：{type(exc).__name__}: {exc}")
        finally:
            try:
                event.stop_event()
            except Exception:  # noqa: BLE001
                pass
    # ---------------------------------------------------------------- 分发逻辑
    async def _handle(self, event: AstrMessageEvent) -> None:
        raw_text = event.get_message_str()
        parsed = self._menu_command_override(raw_text)
        if parsed is None:
            parsed = parse_command(raw_text, self.cfg)
        if parsed is None:
            return

        # 生效平台 / 机器人实例过滤
        if not self._platform_ok(event):
            return

        # 记录交互上下文（设置页「一键获取当前用户 / 当前群」用），
        # 故意放在权限检查之前：这样任何人触发一次就能被获取到。
        self._record_context(event)

        # 需求 12：群友黑名单。被拉黑者一律静默忽略（不回复，避免被刷屏试探）。
        if self._is_blocked(event):
            return

        kind = parsed.kind
        # 私聊 / 群聊范围限制。群开关相关的管理指令必须绕过此限制，
        # 否则群被关闭后主人无法在本群重新开启。
        if kind not in _SCOPE_EXEMPT_KINDS and not self._scope_ok(event):
            await self._hint_group_off(event)
            return
        # 需要主人权限的指令
        if self._needs_master(kind, parsed) and not self._is_master(event):
            await self._reply(event, "🔒 该指令仅主人可用。")
            return

        if kind in (KIND_DRAW, KIND_EDIT):
            await self._draw(event, parsed)
            return
        await self._query(event, parsed)

    def _menu_command_override(self, text: str) -> ParsedCommand | None:
        """在通用解析前锁定菜单入口，避免被内容型功能指令抢先命中。

        菜单分组名与菜单别名都是显式导航入口：完整分组名优先于所有功能指令，
        菜单别名优先于同名的绘画、编辑或其他自动内容指令。只返回菜单相关结果，
        其他消息继续交给 ``parse_command``，因此不会改变其他业务的解析行为。
        """
        raw = _text_of(text).strip()
        if not raw:
            return None
        normalized = re.sub(r"\s+", " ", raw)
        folded = normalized.casefold()

        try:
            sections = self.cfg.menu_sections()
        except Exception:  # noqa: BLE001 - 菜单配置异常时继续使用通用解析
            sections = []
        if not isinstance(sections, (list, tuple)):
            sections = []

        entries = []
        for item in sections:
            if not isinstance(item, dict):
                continue
            name = _text_of(item.get("name")).strip()
            if name:
                entries.append((name, re.sub(r"\s+", " ", name).casefold()))
        entries.sort(key=lambda item: len(item[1]), reverse=True)

        try:
            menu_commands = self.cfg.menu_commands()
        except Exception:  # noqa: BLE001
            menu_commands = []
        if isinstance(menu_commands, str):
            menu_commands = [menu_commands]
        if not isinstance(menu_commands, (list, tuple, set, frozenset)):
            menu_commands = []
        commands = []
        for value in menu_commands:
            command = _text_of(value).strip()
            if command:
                command_folded = re.sub(r"\s+", " ", command).casefold()
                if (command, command_folded) not in commands:
                    commands.append((command, command_folded))
        if not commands:
            commands = [("绘画菜单", "绘画菜单"), ("菜单", "菜单")]
        commands.sort(key=lambda item: len(item[1]), reverse=True)

        for command, command_folded in commands:
            if folded == command_folded:
                return ParsedCommand(kind=KIND_MENU, raw=raw, command=command)

        for name, name_folded in entries:
            if folded == name_folded:
                return ParsedCommand(
                    kind=KIND_MENU_SHOW,
                    arg=name,
                    raw=raw,
                    command=name,
                )

        for command, command_folded in commands:
            # 支持「菜单 分组名」和「菜单分组名」两种写法；分组名按完整名称匹配。
            separator_match = re.match(
                r"^" + re.escape(command_folded) + r"(?:\s+|$)", folded
            )
            if separator_match:
                argument = normalized[separator_match.end():].strip()
                if argument:
                    for name, name_folded in entries:
                        if argument.casefold() == name_folded:
                            return ParsedCommand(
                                kind=KIND_MENU_SHOW,
                                arg=name,
                                raw=raw,
                                command=command,
                            )
                    # 数字和唯一子串仍交给既有菜单详情逻辑处理。
                    try:
                        if self.cfg.get_menu_section(argument) is not None:
                            return ParsedCommand(
                                kind=KIND_MENU_SHOW,
                                arg=argument,
                                raw=raw,
                                command=command,
                            )
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                return ParsedCommand(kind=KIND_MENU, raw=raw, command=command)

            for name, name_folded in entries:
                if folded == command_folded + name_folded:
                    return ParsedCommand(
                        kind=KIND_MENU_SHOW,
                        arg=name,
                        raw=raw,
                        command=command,
                    )
        return None

    def _platform_ok(self, event: AstrMessageEvent) -> bool:
        try:
            name = _text_of(event.get_platform_name())
            pid = _text_of(event.get_platform_id())
        except Exception:  # noqa: BLE001
            return True
        return self.cfg.is_platform_allowed(name, pid)

    async def _hint_group_off(self, event: AstrMessageEvent) -> None:
        """群内指令被群开关拦截时给一次反馈（限流，避免刷屏）。"""
        try:
            group_id = _text_of(event.get_group_id())
        except Exception:  # noqa: BLE001
            return
        if not group_id:
            return
        now = time.monotonic()
        last = self._group_off_hinted.get(group_id, 0.0)
        if now - last < _GROUP_OFF_HINT_INTERVAL:
            return
        self._group_off_hinted[group_id] = now
        if len(self._group_off_hinted) > 256:
            self._group_off_hinted = {
                key: value
                for key, value in self._group_off_hinted.items()
                if now - value < _GROUP_OFF_HINT_INTERVAL
            }
        await self._reply(event, _GROUP_OFF_HINT)

    def _scope_ok(self, event: AstrMessageEvent) -> bool:
        try:
            group_id = _text_of(event.get_group_id())
        except Exception:  # noqa: BLE001
            group_id = ""
        return self.cfg.is_group_allowed(group_id)

    def _is_blocked(self, event: AstrMessageEvent) -> bool:
        """需求 12：判断发送者是否在群友黑名单里。"""
        try:
            sender = _text_of(event.get_sender_id())
            platform_id = _text_of(event.get_platform_id())
        except Exception:  # noqa: BLE001
            return False
        if not sender:
            return False
        try:
            return bool(self.cfg.is_user_blocked(sender, platform_id))
        except Exception:  # noqa: BLE001
            return False

    def _is_master(self, event: AstrMessageEvent) -> bool:
        try:
            user_id = _text_of(event.get_sender_id())
            is_admin = bool(event.is_admin())
        except Exception:  # noqa: BLE001
            return False
        try:
            if self.cfg.is_master(user_id, is_admin):
                return True
        except Exception:  # noqa: BLE001
            pass
        # 兜底：没有配置主人时，允许 AstrBot 管理员在私聊里管理，
        # 避免「一个主人都没配」导致插件完全没法维护。
        # 群聊里不放行：框架管理员可能存在于任意群，放开会越权改全局配置。
        try:
            if not event.is_private_chat():
                return False
        except Exception:  # noqa: BLE001
            return False
        return bool(is_admin)

    # ------------------------------------------------------------ 冷却 / 重试
    def _is_duplicate_event(self, event: AstrMessageEvent) -> bool:
        """判断同一条平台消息是否已处理过（避免重推导致重复扣费）。

        只在能拿到非空且非占位的 ``message_id`` 时生效；拿不到就放行，
        避免因为适配器实现差异误杀正常消息。
        """
        try:
            message_id = _text_of(getattr(event.message_obj, "message_id", ""))
        except Exception:  # noqa: BLE001
            return False
        if not message_id:
            return False
        try:
            platform_id = _text_of(event.get_platform_id())
            origin = _text_of(event.unified_msg_origin)
        except Exception:  # noqa: BLE001
            platform_id = ""
            origin = ""
        key = "%s|%s|%s" % (platform_id, origin, message_id)
        now = time.monotonic()
        seen = self._seen_messages
        if key in seen and now - seen[key] < _MESSAGE_DEDUP_WINDOW:
            logger.info("[gpt-image] 忽略重复消息（%s）" % message_id)
            return True
        seen[key] = now
        if len(seen) > _MESSAGE_DEDUP_MAX:
            # 先丢已过期的；若窗口内消息密集到仍然超限，
            # 再按时间戳保留最新的若干条，保证字典有硬上限。
            pruned = {
                item: stamp
                for item, stamp in seen.items()
                if now - stamp < _MESSAGE_DEDUP_WINDOW
            }
            if len(pruned) > _MESSAGE_DEDUP_MAX:
                keep = sorted(pruned.items(), key=lambda pair: pair[1])[
                    -_MESSAGE_DEDUP_MAX:
                ]
                pruned = dict(keep)
            self._seen_messages = pruned
        return False

    def _cooldown_seconds(self) -> float:
        try:
            return max(0.0, float(self.cfg.cooldown()))
        except Exception:  # noqa: BLE001
            return 0.0

    def _retry_times(self) -> int:
        try:
            return max(0, int(self.cfg.retry_times()))
        except Exception:  # noqa: BLE001
            return 0

    def _cooldown_left(self, event: AstrMessageEvent) -> int:
        """距离下次可用还剩多少秒；0 表示可以立即使用。"""
        limit = self._cooldown_seconds()
        if limit <= 0:
            return 0
        try:
            key = event.unified_msg_origin
        except Exception:  # noqa: BLE001
            return 0
        last = self._cooldowns.get(key)
        if not last:
            return 0
        remain = limit - (time.monotonic() - last)
        if remain <= 0:
            self._cooldowns.pop(key, None)
            return 0
        return int(remain) + (1 if remain % 1 else 0)

    def _mark_cooldown(self, event: AstrMessageEvent) -> None:
        if self._cooldown_seconds() <= 0:
            return
        try:
            self._cooldowns[event.unified_msg_origin] = time.monotonic()
        except Exception:  # noqa: BLE001
            pass
        # 顺手清理过期记录，避免长时间运行内存增长
        if len(self._cooldowns) > 500:
            limit = self._cooldown_seconds()
            now = time.monotonic()
            self._cooldowns = {
                key: value
                for key, value in self._cooldowns.items()
                if now - value < limit
            }

    async def _state_view(self, event: AstrMessageEvent) -> dict:
        try:
            info = await self.state.resolved(event.unified_msg_origin, self.cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[gpt-image] 解析会话状态失败：{exc}")
            info = {}
        if not isinstance(info, dict):
            info = {}
        # 补齐群状态：菜单与提示文案靠它判断「本群 已开启 / 已关闭」，
        # core/state.py 拿不到群号，故在这里按当前事件回填。
        try:
            group_id = _text_of(event.get_group_id())
        except Exception:  # noqa: BLE001
            group_id = ""
        if group_id:
            try:
                info["group_enabled"] = bool(self.cfg.is_group_allowed(group_id))
            except Exception:  # noqa: BLE001
                pass

        # 需求 2：机器人实例级覆盖。某个机器人被单独配置过时，按其实例 ID
        # 重新组装通道 / 协议 / 模型 / 尺寸，未覆盖的字段仍沿用全局值与
        # 会话级覆盖（优先级：会话 > 机器人实例 > 全局）。
        try:
            platform_id = _text_of(event.get_platform_id())
        except Exception:  # noqa: BLE001
            platform_id = ""
        if platform_id:
            try:
                overrides = self.cfg.bot_override(platform_id)
            except Exception:  # noqa: BLE001
                overrides = {}
            if overrides:
                session = info.get("overrides") or {}
                supplier = (
                    _text_of(session.get("supplier"))
                    or _text_of(overrides.get("supplier"))
                    or _text_of(info.get("supplier") or info.get("channel_name"))
                    or self.cfg.active_supplier_name()
                )
                protocol = (
                    _text_of(session.get("protocol"))
                    or _text_of(overrides.get("protocol"))
                    or _text_of(info.get("protocol"))
                )
                model = (
                    _text_of(session.get("model"))
                    or _text_of(overrides.get("model"))
                    or _text_of(info.get("model"))
                )
                size = (
                    _text_of(session.get("size"))
                    or _text_of(overrides.get("size"))
                    or _text_of(info.get("size"))
                )
                try:
                    channel = self.cfg.build_channel(
                        supplier=supplier, protocol=protocol, model=model, platform_id=platform_id
                    )
                except Exception:  # noqa: BLE001
                    channel = None
                if channel is not None:
                    info["channel"] = channel
                    info["channel_name"] = _text_of(getattr(channel, "name", ""))
                    info["supplier"] = info["channel_name"]
                if protocol:
                    info["protocol"] = protocol
                if model:
                    info["model"] = model
                elif channel is not None:
                    info["model"] = _text_of(getattr(channel, "generate_model", ""))
                if size:
                    info["size"] = size
                info["bot_override"] = overrides
        info.update(self._template_vars(event, info))
        return info

    # ------------------------------------------------------------ 绘画 / 编辑
    #: 批量出图数量前缀，例如「4张」「x4」「4 张」
    _BATCH_RE = re.compile(r"^(?:[xX×]\s*)?(\d{1,2})\s*(?:张|幅|个|份|pics?|images?)\s*", re.IGNORECASE)

    #: 「自动尺寸」的各种写法
    _AUTO_SIZES = ("auto", "自动", "")

    def _parse_batch(self, text: str) -> tuple:
        """解析「4张 一只猫」形式，返回 (张数, 剩余提示词)。"""
        raw = _text_of(text)
        matched = self._BATCH_RE.match(raw)
        if not matched:
            return 1, raw
        try:
            count = int(matched.group(1))
        except (TypeError, ValueError):
            return 1, raw
        remain = raw[matched.end():].strip()
        if not remain:
            # 只写了数量没写内容：交给上层给出示例提示
            return max(1, count), ""
        return max(1, count), remain

    @staticmethod
    def _split_prompts(text: str) -> list:
        """多行提示词拆分成多条；只有一行时返回单元素列表。"""
        raw = _text_of(text)
        if "\n" not in raw:
            return [raw] if raw else []
        lines = [line.strip() for line in raw.split("\n")]
        lines = [line for line in lines if len(line) >= 2]
        if len(lines) <= 1:
            return [raw.replace("\n", " ").strip()] if raw else []
        return lines

    def _resolve_size(self, info: dict, prompt: str) -> tuple:
        """解析本次实际使用的尺寸，返回 (尺寸, 提示词, 说明)。

        仅在「当前尺寸为 auto」且开启了自动识别时，才从提示词里提取
        「9:16」「4K」「2048x2048」这类信息（需求 15）。
        """
        size = _text_of(info.get("size")) or self.cfg.active_size()
        protocol = _text_of(info.get("protocol")) or self.cfg.active_protocol()
        note = ""
        auto = str(size or "").strip().lower() in self._AUTO_SIZES
        if auto and self.cfg.size_auto_detect():
            hint = extract_size_hints(prompt)
            detected = _text_of(hint.get("size"))
            if detected:
                size = detected
                note = "（已按提示词识别为 %s）" % detected
                stripped = strip_size_hints(prompt)
                if stripped.strip():
                    prompt = stripped
        if size and size != "auto" and not protocol_supports_size(protocol, size):
            # 协议能力矩阵只能作为提示，不能覆盖用户已经选定的尺寸。
            # OpenAI 兼容中转站和自部署协议经常扩展官方尺寸；静默回退到
            # 1024x1024 会造成“切换成功但实际请求仍是默认尺寸”的错觉。
            note = "（%s 可能不接受 %s，将按已选尺寸发送）" % (protocol or "当前协议", size)
        return size, prompt, note

    async def _maybe_translate(self, channel: Any, prompt: str, is_edit: bool) -> str:
        """需求 7：按开关把中文口语改写成结构化英文提示词；失败原样返回。"""
        if not prompt or not self.cfg.translate_enabled():
            return prompt
        try:
            target = channel
            supplier = self.cfg.translate_supplier()
            if supplier and supplier != _text_of(getattr(channel, "name", "")):
                built = self.cfg.build_channel(supplier=supplier)
                if built is not None:
                    target = built
            model = self.cfg.translate_model() or _text_of(getattr(target, "generate_model", ""))
            system = self.cfg.translate_prompt_text() or ""
            text = await translate_prompt(
                target,
                model,
                prompt,
                system,
                self.cfg.timeout(),
                self.cfg.proxy(),
            )
            if text:
                logger.info("[gpt-image] 提示词已由文本模型改写")
                return text
        except Exception as exc:  # noqa: BLE001 - 翻译失败必须不影响出图
            logger.warning(f"[gpt-image] 提示词改写失败，沿用原文：{exc}")
        if is_edit:
            return prompt
        try:
            fallback = build_structured_prompt(prompt)
            return fallback or prompt
        except Exception:  # noqa: BLE001
            return prompt

    def _flavor_line(self) -> str:
        """需求 10：随机取一条出图文案；未开启时返回空串。"""
        if not self.cfg.flavor_enabled():
            return ""
        try:
            return _text_of(pick_flavor_line(self.cfg.flavor_lines()))
        except Exception:  # noqa: BLE001
            return ""

    def _protocol_of(self, info: dict) -> str:
        return _text_of(info.get("protocol")) or self.cfg.active_protocol()

    async def _draw(self, event: AstrMessageEvent, parsed: ParsedCommand) -> None:
        """执行一次绘画/编辑会话，并统一收口取消统计。"""
        task_id = uuid.uuid4().hex[:12]
        try:
            await self._draw_impl(event, parsed, task_id)
        except asyncio.CancelledError:
            if self.stats.has_session(task_id):
                self.stats.record_cancel(
                    session_id=task_id,
                    kind="edit" if parsed.kind == KIND_EDIT else "draw",
                    error="任务被取消",
                )
            raise
        except Exception as exc:  # noqa: BLE001 - 交给上层回复，同时补齐异常失败统计
            if self.stats.has_session(task_id):
                self.stats.record_failure(
                    session_id=task_id,
                    kind="edit" if parsed.kind == KIND_EDIT else "draw",
                    error="%s: %s" % (type(exc).__name__, exc),
                )
            raise

    async def _draw_impl(
        self, event: AstrMessageEvent, parsed: ParsedCommand, task_id: str
    ) -> None:
        # 冷却检查：同一会话冷却期内重复触发直接提示，避免刷屏与浪费额度
        cooldown_left = self._cooldown_left(event)
        if cooldown_left > 0:
            await self._reply(event, _COOLDOWN_HINT.format(seconds=cooldown_left))
            return

        info = await self._state_view(event)
        channel = info.get("channel")
        ready, reason = _channel_ready(channel)
        if not ready:
            await self._reply(event, reason)
            return

        is_edit = parsed.kind == KIND_EDIT
        prompt = _clean_prompt(parsed.arg)
        # 图片链接既可能是编辑素材，也可能只是附带的参考地址；
        # 这里统一把它们从提示词中剥离，避免把 URL 当成画面内容。
        stripped, links = await extract_image_urls(prompt)
        if links:
            prompt = _clean_prompt(stripped)

        images: list = []
        batch_count = 1
        if is_edit:
            try:
                images = await collect_input_images(event, self.cfg, include_mentions=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[gpt-image] 采集编辑素材失败：{exc}")
                images = []
            if not images:
                await self._reply(
                    event,
                    "🖼 图片编辑需要一张图片作为素材，任选一种方式：\n"
                    "① 直接把图片发给我，并带上编辑指令；\n"
                    "② 引用一张图片后发送「图片编辑 + 内容」；\n"
                    "③ 在指令后附上图片链接；\n"
                    "④ 发送「图片编辑 + 内容 + @某人」使用对方头像（OneBot v11）。",
                )
                return
            if not prompt:
                await self._reply(event, "✍️ 请补上编辑要求，例如：图片编辑把背景换成夜景")
                return
        else:
            if not prompt:
                menu = self.cfg.menu_commands()
                hint = menu[0] if menu else "菜单"
                await self._reply(
                    event,
                    f"✍️ 请告诉我你想画什么，例如：绘画一只在月球上钓鱼的猫\n"
                    f"💡 批量出图：绘画 4张 一只猫\n"
                    f"（发送「{hint}」查看完整用法）",
                )
                return
            if links:
                # 文生图指令里带了图片链接：视为参考图，转为编辑请求
                try:
                    images = await collect_input_images(event, self.cfg)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[gpt-image] 采集参考图失败：{exc}")
                    images = []
                is_edit = bool(images)
            else:
                # 需求 3：批量出图（「4张 xxx」或一次给多行提示词）
                batch_count, remain = self._parse_batch(prompt)
                if not remain:
                    await self._reply(
                        event,
                        "✍️ 批量出图请写成：绘画 4张 一只猫\n"
                        "也可以一次给我多行提示词，我会分别出图。",
                    )
                    return
                prompt = remain

        limit = max(1, self.cfg.batch_max())
        prompts = self._split_prompts(prompt) if not is_edit else [prompt]
        if not prompts:
            await self._reply(event, "✍️ 提示词不能为空。")
            return
        if batch_count > 1 and len(prompts) == 1:
            prompts = [prompts[0]] * batch_count
        if len(prompts) > limit:
            await self._reply(
                event,
                "⚠️ 一次最多出 %d 张，已按上限处理（可在插件设置页调整「批量出图上限」）。" % limit,
            )
            prompts = prompts[:limit]

        size, prompts[0], size_note = self._resolve_size(info, prompts[0])
        override = _text_of((info.get("overrides") or {}).get("model"))
        if override:
            model = override
        elif is_edit:
            model = channel.edit_model_name or channel.generate_model
        else:
            model = channel.generate_model

        protocol = self._protocol_of(info)
        variables = self._template_vars(
            event,
            info,
            prompt=prompts[0],
            original_prompt=prompts[0],
            final_prompt="",
            prompt_type="图片编辑" if is_edit else "绘画",
            count=len(prompts),
            input_count=len(images),
            batch_count=len(prompts),
            size=size,
            model=model,
            mode="edit" if is_edit else "draw",
            task_id=task_id,
            request_id=task_id,
            batch_index=1,
            batch_total=len(prompts),
            success_count=0,
            failure_count=0,
            retry_count=0,
            failure_reason="",
            started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.stats.record_start(
            session_id=task_id,
            kind="edit" if is_edit else "draw",
            model=model,
            protocol=protocol,
            supplier=_text_of(getattr(channel, "name", "")),
            requested=len(prompts),
        )
        try:
            await event.send(event.plain_result(render_template(self.cfg.start_prompt(), **variables)))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[gpt-image] 发送开始提示失败：{exc}")

        started = time.monotonic()
        self._mark_cooldown(event)
        semaphore = self._acquire()
        results: list = []
        failures: list = []
        attempts_allowed = 1 + self._retry_times()
        retry_count = 0

        for index, item_prompt in enumerate(prompts):
            if index > 0:
                item_size, item_prompt, _note = self._resolve_size(info, item_prompt)
            else:
                item_size = size
            final_prompt = await self._maybe_translate(channel, item_prompt, is_edit)
            request = GenerateRequest(
                prompt=final_prompt,
                images=images,
                size=item_size,
                model=model,
                n=1,
                quality=self.cfg.quality(),
                transparent_background=self.cfg.transparent_background(),
            )
            protocol_object = None
            last_error: Any = None
            try:
                async with semaphore:
                    for attempt in range(attempts_allowed):
                        try:
                            protocol_object = create_protocol(
                                channel, self.cfg.timeout(), self.cfg.proxy()
                            )
                            result = await protocol_object.generate(request)
                            if result is not None and not result.is_empty():
                                results.append((result, item_prompt, item_size, final_prompt))
                                last_error = None
                            else:
                                last_error = ProtocolError("接口没有返回图片")
                            break
                        except Exception as exc:  # noqa: BLE001 - 逐次判断是否重试
                            last_error = exc
                            if attempt + 1 >= attempts_allowed or not _should_retry(exc):
                                break
                            logger.warning(
                                "[gpt-image] 第 %d 次请求失败，准备重试：%s" % (attempt + 1, exc)
                            )
                            retry_count += 1
                        finally:
                            if protocol_object is not None:
                                try:
                                    await protocol_object.close()
                                except Exception:  # noqa: BLE001
                                    pass
                                protocol_object = None
            finally:
                if protocol_object is not None:
                    try:
                        await protocol_object.close()
                    except Exception:  # noqa: BLE001
                        pass
            if last_error is not None:
                failures.append((item_prompt, last_error))
                continue

        elapsed = time.monotonic() - started

        if not results:
            first_error = failures[0][1] if failures else ProtocolError("接口没有返回图片")
            self.stats.record_failure(
                session_id=task_id,
                kind="edit" if is_edit else "draw",
                seconds=elapsed,
                model=model,
                protocol=protocol,
                supplier=_text_of(getattr(channel, "name", "")),
                requested=len(prompts),
                succeeded=0,
                retries=retry_count,
                error="%s: %s" % (type(first_error).__name__, first_error),
            )
            if isinstance(first_error, asyncio.TimeoutError):
                await self._reply(
                    event,
                    f"❌ 生成超时（>{self.cfg.timeout():.0f} 秒），请稍后再试。",
                )
                return
            if isinstance(first_error, ProtocolError):
                await self._reply(
                    event,
                    f"❌ 生成失败：{first_error}{_auth_hint(channel, first_error)}",
                )
                return
            logger.exception(f"[gpt-image] 调用绘画接口失败：{first_error}")
            await self._reply(
                event,
                f"❌ 生成失败：{type(first_error).__name__}: {first_error}",
            )
            return

        total_images = 0
        model_used = model
        for result, _item_prompt, _item_size, _final_prompt in results:
            total_images += max(1, result.count)
            model_used = _text_of(result.model) or model_used
        variables = self._template_vars(
            event,
            info,
            elapsed=elapsed,
            count=total_images,
            batch_count=len(prompts),
            input_count=len(images),
            failed_count=len(failures),
            failure_count=len(failures),
            success_count=total_images,
            batch_index=len(prompts),
            batch_total=len(prompts),
            retry_count=retry_count,
            failure_reason=(str(failures[0][1]) if failures else ""),
            task_id=task_id,
            request_id=task_id,
            started_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - elapsed)),
            finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            elapsed_ms=round(elapsed * 1000, 2),
            model=model_used,
            size=results[0][2],
            prompt=results[0][1],
            original_prompt=results[0][1],
            final_prompt=results[0][3],
            mode="edit" if is_edit else "draw",
            prompt_type="图片编辑" if is_edit else "绘画",
        )
        caption = render_template(self.cfg.done_prompt(), **variables)
        if size_note:
            caption = caption + "\n" + size_note
        flavor = self._flavor_line()
        if flavor:
            caption = caption + "\n" + render_template(flavor, **variables)
        try:
            self._last_requests[event.unified_msg_origin] = {
                "prompt": prompts[0],
                "size": size,
                "is_edit": is_edit,
                "time": time.time(),
            }
        except Exception:  # noqa: BLE001
            pass

        delivery_failures = 0
        for result, _item_prompt, _item_size, _final_prompt in results:
            try:
                delivered = await send_result(
                    event, result, caption, file_fallback=self.cfg.qq_file_fallback()
                )
                if not delivered:
                    delivery_failures += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"[gpt-image] 发送图片失败：{exc}")
                delivery_failures += 1
            caption = ""
        if failures or delivery_failures:
            failure_text = (
                "%s: %s" % (type(failures[0][1]).__name__, failures[0][1])
                if failures
                else "图片生成成功，但发送失败（失败 %d 项）" % delivery_failures
            )
            self.stats.record_failure(
                session_id=task_id,
                kind="edit" if is_edit else "draw",
                seconds=elapsed,
                model=model_used,
                protocol=protocol,
                supplier=_text_of(getattr(channel, "name", "")),
                images=total_images,
                requested=len(prompts),
                succeeded=max(0, len(results) - delivery_failures),
                retries=retry_count,
                error=failure_text,
            )
        else:
            self.stats.record_success(
                kind="edit" if is_edit else "draw",
                seconds=elapsed,
                model=model_used,
                protocol=protocol,
                supplier=_text_of(getattr(channel, "name", "")),
                images=total_images,
                session_id=task_id,
                requested=len(prompts),
                retries=retry_count,
            )
        if not delivery_failures:
            try:
                self.fun.record_draw(
                    _text_of(event.get_sender_id()),
                    _text_of(event.get_sender_name()),
                    total_images,
                )
            except Exception as exc:  # noqa: BLE001 - 统计失败不影响出图
                logger.warning(f"[gpt-image] 记录抽卡 / 排行统计失败：{exc}")
        if failures or delivery_failures:
            notices = []
            if failures:
                notices.append(
                    "有 %d 项没能生成成功（%s）" % (len(failures), failures[0][1])
                )
            if delivery_failures:
                notices.append(
                    "有 %d 项已生成但投递失败；已尝试图片、文件和链接兜底，请检查平台媒体限制"
                    % delivery_failures
                )
            await self._reply(event, "⚠️ " + "；".join(notices))

    # ------------------------------------------------------- 菜单 / 查询与切换
    async def _query(self, event: AstrMessageEvent, parsed: ParsedCommand) -> None:
        kind = parsed.kind
        info = await self._state_view(event)
        umo = event.unified_msg_origin

        if kind == KIND_MENU:
            # 无参数的「菜单」显示总菜单；带分组参数的消息由 KIND_MENU_SHOW 处理。
            await self._reply(event, build_menu(self.cfg, info, is_master=self._is_master(event)))
            return
        if kind == KIND_HELP:
            await self._reply(event, build_menu(self.cfg, info, is_master=self._is_master(event)))
            return
        if kind == KIND_MODELS:
            await self._list_models(event, parsed, info)
            return
        if kind == KIND_SIZES:
            await self._list_sizes(event, info)
            return
        if kind == KIND_PROTOCOLS:
            await self._list_protocols(event, info)
            return
        if kind == KIND_SUPPLIERS:
            await self._list_suppliers(event, info)
            return
        if kind == KIND_STATS:
            await self._show_stats(event)
            return
        if kind == KIND_RESET:
            await self._reset_session(event, info, umo)
            return
        if kind == KIND_GROUPS:
            await self._groups(event, parsed, info)
            return
        if kind == KIND_SWITCH_MODEL:
            await self._switch_model(event, parsed, info, umo)
            return
        if kind == KIND_SWITCH_SIZE:
            await self._switch_size(event, parsed, info, umo)
            return
        if kind == KIND_SWITCH_PROTOCOL:
            await self._switch_protocol(event, parsed, info, umo)
            return
        if kind == KIND_SWITCH_SUPPLIER:
            await self._switch_supplier(event, parsed, info, umo)
            return
        if kind == KIND_ADD_SIZE:
            await self._add_size(event, parsed, info)
            return
        if kind == KIND_DEL_SIZE:
            await self._del_size(event, parsed, info)
            return
        if kind in (KIND_GROUP_ON, KIND_GROUP_OFF):
            await self._toggle_group(event, kind == KIND_GROUP_ON)
            return
        if kind == KIND_MASTER_ADD:
            await self._master_change(event, parsed, add=True)
            return
        if kind == KIND_MASTER_DEL:
            await self._master_change(event, parsed, add=False)
            return
        if kind == KIND_RELOAD:
            self.cfg.reload()
            await self._reply(event, "♻️ 已重新读取插件配置。")
            return
        if kind == KIND_WHOAMI:
            await self._whoami(event, info)
            return
        if kind == KIND_MENU_LIST:
            await self._menu_list(event)
            return
        if kind == KIND_MENU_SHOW:
            await self._menu_show(event, parsed)
            return
        if kind == KIND_MENU_SET:
            await self._menu_set(event, parsed)
            return
        if kind == KIND_MENU_DEL:
            await self._menu_del(event, parsed)
            return
        if kind == KIND_PRESET_LIST:
            if _clean_prompt(parsed.arg):
                await self._preset_apply(event, parsed, info)
            else:
                await self._preset_list(event)
            return
        if kind == KIND_PRESET_ADD:
            await self._preset_add(event, parsed)
            return
        if kind == KIND_PRESET_DEL:
            await self._preset_del(event, parsed)
            return
        if kind == KIND_GACHA:
            await self._gacha(event, info)
            return
        if kind == KIND_RANK:
            await self._rank(event)
            return
        if kind == KIND_IMG2PROMPT:
            await self._img2prompt(event, info)
            return
        if kind == KIND_PROMPT_TOOLS:
            await self._prompt_tools(event)
            return
        if kind == KIND_REPEAT:
            await self._repeat(event, parsed, info, umo)
            return

    # ------------------------------------------------------------------ 菜单项
    def _requires_master(self, kind: str) -> bool:
        return is_master_kind(kind)

    def _needs_master(self, kind: str, parsed: Any = None) -> bool:
        """判断指令是否需要主人权限。

        ``kind`` 为 ``groups`` 时同时承载「群列表（所有人）」与「群开关（主人）」，
        因此需要按实际命中的指令名二次判定，避免普通用户看不了群列表。
        """
        if kind in _EXTRA_MASTER_KINDS:
            return True
        if kind == KIND_GROUPS and parsed is not None:
            command = _text_of(getattr(parsed, "command", "")).lower()
            if command:
                try:
                    master_commands = [item.lower() for item in self.cfg.master_commands()]
                except Exception:  # noqa: BLE001
                    master_commands = []
                return command in master_commands
            return False
        return is_master_kind(kind)

    async def _preset_list(self, event: AstrMessageEvent) -> None:
        """需求 14：列出内置提示词库；带参数时直接套用某条预设出图。"""
        prompts = self.cfg.prompts()
        if not prompts:
            await self._reply(
                event,
                "📭 提示词库是空的。\n主人可用「添加预设 <名称> <内容>」补充。",
            )
            return
        names = [str(item.get("name") or "") for item in prompts]
        lines = [
            render_numbered_list("📚 内置提示词库", names),
            "",
            "用法：发送「预设 <序号或名称> 你的补充要求」即可套用该提示词出图。",
        ]
        if self._is_master(event):
            lines.append("主人可用「添加预设 <名称> <内容>」「删除预设 <名称>」维护。")
        await self._reply(event, "\n".join(lines))

    async def _preset_add(self, event: AstrMessageEvent, parsed: ParsedCommand) -> None:
        """需求 17：主人新增内置提示词。"""
        raw = _clean_prompt(parsed.arg)
        if not raw:
            await self._reply(
                event,
                "用法：添加预设 <名称> <提示词正文>\n"
                "示例：添加预设 赛博猫咪 一只赛博朋克风格的猫，霓虹灯，雨夜街道，电影感光影",
            )
            return
        parts = re.split(r"[\s　]+", raw, maxsplit=1)
        name = parts[0].strip()
        body = parts[1].strip() if len(parts) > 1 else ""
        if not body:
            await self._reply(event, "❌ 请补上提示词正文，例如：添加预设 赛博猫咪 一只赛博朋克风格的猫")
            return
        if self.cfg.add_prompt(name, body):
            self.cfg.save()
            await self._reply(event, "✅ 已添加预设：%s\n（共 %d 条）" % (name, len(self.cfg.prompts())))
        else:
            await self._reply(event, "❌ 添加失败：名称已被占用或内容为空。")

    async def _preset_del(self, event: AstrMessageEvent, parsed: ParsedCommand) -> None:
        """需求 17：主人删除内置提示词（含出厂预设）。"""
        target = _clean_prompt(parsed.arg)
        if not target:
            await self._reply(event, "用法：删除预设 <序号或名称>")
            return
        entry = self.cfg.get_prompt(target)
        if entry is None:
            await self._reply(event, "❌ 没找到预设：%s" % target)
            return
        name = str(entry.get("name") or "")
        if self.cfg.remove_prompt(name):
            self.cfg.save()
            await self._reply(event, "✅ 已删除预设：%s" % name)
        else:
            await self._reply(event, "❌ 删除失败：%s" % target)

    async def _prompt_tools(self, event: AstrMessageEvent) -> None:
        """提示词工具入口：汇集所有与提示词库有关的指令。"""
        lines = [
            "🧰 提示词工具",
            "━━━━━━━━━━━━━━━",
            "· 「预设列表」查看全部内置提示词",
            "· 「预设 <序号或名称> 补充要求」套用预设出图",
            "· 「图片转提示词」反推图片的提示词（需开启对应开关）",
        ]
        if self._is_master(event):
            lines.append("· 「添加预设 <名称> <内容>」新增预设（主人）")
            lines.append("· 「删除预设 <名称>」删除预设（主人）")
        await self._reply(event, "\n".join(lines))

    async def _menu_list(self, event: AstrMessageEvent) -> None:
        """必修②：列出全部菜单分组。"""
        sections = self.cfg.menu_sections()
        if not sections:
            await self._reply(
                event,
                "目前没有菜单分组。\n"
                "主人可用「设置菜单 <分组名> <指令1|指令2>」新建，"
                "或「重置菜单」恢复出厂分组。",
            )
            return
        lines = [render_menu_sections(self.cfg, compact=False)]
        if self._is_master(event):
            lines.append("")
            lines.append("主人可用「设置菜单 <分组名> <指令1|指令2>」修改，用「删除菜单 <分组名>」删除。")
        await self._reply(event, "\n".join(lines))

    async def _menu_show(self, event: AstrMessageEvent, parsed: ParsedCommand) -> None:
        """必修②：查看单个菜单分组（内容型指令，支持「菜单 模型」连写）。"""
        target = _clean_prompt(parsed.arg)
        if not target:
            await self._menu_list(event)
            return
        rendered = build_section_menu(self.cfg, target)
        if rendered is None:
            names = self.cfg.menu_section_names()
            await self._reply(
                event,
                "没找到菜单分组「%s」。\n%s"
                % (target, render_numbered_list("📂 可用分组", names)),
            )
            return
        await self._reply(event, rendered)

    async def _menu_set(self, event: AstrMessageEvent, parsed: ParsedCommand) -> None:
        """必修②：主人设置某个菜单分组的内容。"""
        raw = _clean_prompt(parsed.arg)
        if not raw:
            await self._reply(
                event,
                "用法：设置菜单 <分组名> <指令1|指令2|指令3>\n"
                "示例：设置菜单 模型功能 模型列表|切换模型|协议列表|切换协议\n"
                "（用 | 或 、 分隔多条指令；发送「菜单列表」查看现有分组）",
            )
            return
        parts = re.split(r"[\s　]+", raw, maxsplit=1)
        name = parts[0].strip()
        body = parts[1].strip() if len(parts) > 1 else ""
        if not name:
            await self._reply(event, "请给出分组名。")
            return
        if body:
            items = [item.strip() for item in re.split(r"[|｜,，、]+", body)]
            items = [item for item in items if item]
        else:
            items = []
        if self.cfg.set_menu_section(name, items):
            self.cfg.save()
            detail = "、".join(items) if items else "（空分组）"
            await self._reply(
                event,
                "已设置菜单分组「%s」：%s\n发送「菜单列表」查看全部。" % (name, detail),
            )
        else:
            await self._reply(event, "设置失败：分组过多或名称为空。")

    async def _menu_del(self, event: AstrMessageEvent, parsed: ParsedCommand) -> None:
        """必修②：主人删除菜单分组。"""
        target = _clean_prompt(parsed.arg)
        if not target:
            await self._reply(event, "用法：删除菜单 <分组名>")
            return
        entry = self.cfg.get_menu_section(target)
        if entry is None:
            await self._reply(event, "没找到菜单分组：%s" % target)
            return
        name = str(entry.get("name") or "")
        if self.cfg.remove_menu_section(name):
            self.cfg.save()
            await self._reply(event, "已删除菜单分组：%s" % name)
        else:
            await self._reply(event, "删除失败：%s" % target)

    async def _repeat(
        self, event: AstrMessageEvent, parsed: ParsedCommand, info: dict, umo: str
    ) -> None:
        """重画：复用本会话最近一次的提示词重新出图。"""
        record = self._last_requests.get(umo)
        if not record:
            await self._reply(event, "ℹ️ 本会话还没有出图记录，先画一张试试吧。")
            return
        extra = _clean_prompt(parsed.arg)
        prompt = str(record.get("prompt") or "")
        if extra:
            prompt = (prompt + " " + extra).strip()
        if not prompt:
            await self._reply(event, "ℹ️ 上一次的提示词已丢失，请重新描述。")
            return
        kind = KIND_EDIT if record.get("is_edit") else KIND_DRAW
        await self._draw(
            event,
            ParsedCommand(kind=kind, arg=prompt, raw=parsed.raw, command=parsed.command),
        )

    async def _whoami(self, event: AstrMessageEvent, info: dict) -> None:
        """回复当前会话的用户 ID / 群 ID / 平台与机器人实例信息。

        用于让用户把 ID 填进设置页的主人列表与群列表，免去翻 AstrBot 日志。
        """
        def _safe(getter_name: str) -> str:
            getter = getattr(event, getter_name, None)
            if not callable(getter):
                return ""
            try:
                return _text_of(getter()).strip()
            except Exception:  # noqa: BLE001 - 不同适配器实现差异较大
                return ""

        user_id = _safe("get_sender_id")
        user_name = _safe("get_sender_name")
        group_id = _safe("get_group_id")
        platform = _safe("get_platform_name")
        platform_id = _safe("get_platform_id")
        self_id = _safe("get_self_id")

        try:
            is_private = bool(event.is_private_chat())
        except Exception:  # noqa: BLE001
            is_private = not group_id

        members = []
        try:
            members = list(self.cfg.masters())
        except Exception:  # noqa: BLE001
            members = []

        readonly = bool(members) and user_id in members
        if readonly:
            role_text = "✅ 是插件主人（来自主人列表）"
        elif self._is_master(event):
            role_text = "✅ 是插件主人（AstrBot 管理员自动授权）"
        else:
            role_text = "— 普通用户（如需管理指令，请把下面的用户 ID 填入设置页的主人列表）"

        channel = info.get("channel")
        lines = [
            "🆔 你的身份信息",
            "━━━━━━━━━━━━━━━━━━━━",
            f"· 用户 ID：{user_id or '（未获取到）'}",
            f"· 昵称：{user_name or '（未获取到）'}",
        ]
        if group_id:
            lines.append(f"· 群 ID：{group_id}")
        elif is_private:
            lines.append("· 群 ID：（当前为私聊，请在群内发送本指令获取群 ID）")
        else:
            lines.append("· 群 ID：（未获取到）")
        lines.extend(
            [
                f"· 平台类型：{platform or '（未获取到）'}",
                f"· 机器人实例 ID：{platform_id or '（未获取到）'}",
            ]
        )
        if self_id:
            lines.append(f"· 机器人账号 ID：{self_id}")
        lines.append(f"· 插件身份：{role_text}")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("用法：把「用户 ID」填入设置页的主人列表即可使用管理指令；")
        lines.append("把「群 ID」填入群列表可配合白名单 / 黑名单模式使用。")
        if channel is not None:
            lines.append(
                "当前供应商：%s · 协议：%s · 模型：%s"
                % (
                    _text_of(getattr(channel, "name", "")) or "—",
                    protocol_label(getattr(channel, "protocol", "")),
                    _text_of(getattr(channel, "generate_model", "")) or "—",
                )
            )
        await self._reply(event, "\n".join(lines))

    async def _list_models(self, event: AstrMessageEvent, parsed: ParsedCommand, info: dict) -> None:
        """列出当前协议下的模型：优先本地缓存，主人可用「模型列表 刷新」联网重拉。"""
        protocol = _text_of(info.get("protocol")) or self.cfg.active_protocol()
        supplier = _text_of(info.get("supplier") or info.get("channel_name"))
        if not supplier:
            supplier = self.cfg.active_supplier_name()
        is_master = self._is_master(event)
        refresh = _clean_prompt(parsed.arg).lower() in ("刷新", "refresh", "更新", "-r")
        if refresh and not is_master:
            refresh = False

        channel = info.get("channel")
        if channel is None:
            channel = self.cfg.build_channel(supplier=supplier, protocol=protocol)
        if channel is None:
            channel = self.cfg.active_channel()
        effective_supplier = _text_of(getattr(channel, "name", "")) or supplier

        options = self._model_options(protocol, effective_supplier)
        cached = []
        try:
            scoped_cache = getattr(self.cfg, "model_cache_for_supplier", None)
            if callable(scoped_cache):
                cached = list(scoped_cache(protocol, effective_supplier) or [])
            else:
                cached = list(self.cfg.model_cache(protocol) or [])
        except Exception:  # noqa: BLE001
            cached = []
        message = ""
        # 没有远端缓存时联网拉取一次；已有缓存时只有主人「刷新」才重新联网
        if refresh or not cached:
            ready, reason = _channel_ready(channel)
            if not ready:
                if not options:
                    await self._reply(event, reason)
                    return
            else:
                protocol_obj = None
                try:
                    protocol_obj = create_protocol(
                        channel, self.cfg.timeout(), self.cfg.proxy()
                    )
                    models = await protocol_obj.list_models()
                except ProtocolError as exc:
                    message = "⚠️ 联网获取失败：%s" % exc
                    models = []
                except Exception as exc:  # noqa: BLE001
                    message = "⚠️ 联网获取失败：%s: %s" % (type(exc).__name__, exc)
                    models = []
                finally:
                    if protocol_obj is not None:
                        try:
                            await protocol_obj.close()
                        except Exception:  # noqa: BLE001
                            pass
                fetched = []
                for item in models or []:
                    text = _text_of(item).strip()
                    if text and text not in fetched:
                        fetched.append(text)
                if fetched:
                    merged = list(fetched)
                    for item in options:
                        if item not in merged:
                            merged.append(item)
                    try:
                        scoped_setter = getattr(self.cfg, "set_model_cache_for_supplier", None)
                        if callable(scoped_setter):
                            scoped_setter(protocol, effective_supplier, merged)
                        else:
                            self.cfg.set_model_cache(protocol, merged)
                        self.cfg.save()
                    except Exception:  # noqa: BLE001
                        pass
                    # 缓存写入后重新推导候选：models_for() 的顺序是
                    # 「生成模型、编辑模型、远端缓存」，这里必须用同一个函数，
                    # 否则菜单里显示的序号和「切换模型 <序号>」命中的模型会对不上。
                    options = self._model_options(protocol, effective_supplier)

        current = _text_of(info.get("model")) or self.cfg.model_for(protocol)
        lines = [
            render_numbered_list("🧠 %s 可用模型" % PROTOCOL_SHORT_LABELS.get(protocol, protocol), options, current),
        ]
        if message:
            lines.append(message)
        lines.append("")
        lines.append("发送「切换模型 <序号或名称>」即可切换（仅当前会话生效）。")
        if is_master:
            lines.append("主人发送「模型列表 刷新」可联网重新拉取模型列表。")
        await self._reply(event, "\n".join(lines))

    async def _list_sizes(self, event: AstrMessageEvent, info: dict) -> None:
        sizes = self.cfg.image_sizes()
        current = _text_of(info.get("size")) or self.cfg.active_size()
        lines = [
            render_numbered_list("📐 可用尺寸", sizes, current),
            "",
            "发送「切换尺寸 <序号或尺寸>」切换；主人可用「添加尺寸 / 删除尺寸」维护列表。",
        ]
        await self._reply(event, "\n".join(lines))

    async def _list_protocols(self, event: AstrMessageEvent, info: dict) -> None:
        current_key = _text_of(info.get("protocol")) or self.cfg.active_protocol()
        rows = []
        for key in PROTOCOL_ORDER:
            model = self.cfg.model_for(key) or default_model_for(key)
            rows.append("%s（%s）· 模型 %s" % (PROTOCOL_SHORT_LABELS.get(key, key), key, model))
        supplier = _text_of(info.get("supplier") or info.get("channel_name")) or self.cfg.active_supplier_name()
        lines = [
            render_numbered_list("🔌 接口协议", rows, self._protocol_row(info)),
            "",
            f"当前供应商：{supplier or '—'}（三类协议共用同一份地址与密钥）",
            "发送「切换协议 <序号或名称>」切换当前会话使用的协议。",
        ]
        await self._reply(event, "\n".join(lines))

    async def _groups(
        self, event: AstrMessageEvent, parsed: ParsedCommand, info: dict
    ) -> None:
        """「群开关」用于开关本群，「群列表」用于查看。"""
        master_commands = [item.lower() for item in self.cfg.master_commands()]
        is_switch = _text_of(parsed.command).lower() in master_commands
        if not is_switch:
            await self._list_groups(event, info)
            return

        arg = _clean_prompt(parsed.arg).lower()
        if arg in ("开", "on", "开启", "打开", "enable", "true", "1"):
            await self._toggle_group(event, True)
            return
        if arg in ("关", "off", "关闭", "disable", "false", "0"):
            await self._toggle_group(event, False)
            return
        if arg:
            await self._reply(
                event,
                "用法：群开关 / 群开关 开 / 群开关 关\n不传参数时会在当前状态之间切换。",
            )
            return

        # 不带参数：按当前状态取反
        try:
            group_id = _text_of(event.get_group_id())
        except Exception:  # noqa: BLE001
            group_id = ""
        if not group_id:
            await self._list_groups(event, info)
            return
        await self._toggle_group(event, not self.cfg.is_group_allowed(group_id))

    async def _list_groups(self, event: AstrMessageEvent, info: dict) -> None:
        mode = self.cfg.group_mode()
        groups = self.cfg.group_list()
        current_group = ""
        try:
            current_group = _text_of(event.get_group_id())
        except Exception:  # noqa: BLE001
            current_group = ""
        lines = [f"👥 群聊模式：{_GROUP_MODE_LABELS.get(mode, mode)}"]
        if current_group:
            enabled = self.cfg.is_group_allowed(current_group)
            lines.append(f"本群（{current_group}）：{'✅ 已开启' if enabled else '🚫 已关闭'}")
        if groups:
            lines.append(render_numbered_list("📋 群列表", groups, current_group))
        elif mode != "all":
            lines.append("📋 群列表（暂无）")
        if current_group:
            lines.append("")
            lines.append(f"🆔 本群 ID：{current_group}")
            lines.append("主人发送「群开关」或在设置页填写该 ID，即可开关本群绘画功能。")
            lines.append("（OneBot v11 为真实群号；QQ 官方机器人 为 group_openid，两者都可直接用）")
        else:
            lines.append("")
            lines.append("私聊中无法获取群信息，请在群内发送本指令。")
        await self._reply(event, "\n".join(lines))
    # ------------------------------------------------------------ 切换类动作
    async def _switch_model(
        self, event: AstrMessageEvent, parsed: ParsedCommand, info: dict, umo: str
    ) -> None:
        target, for_all = _split_global(parsed.arg)
        target = _clean_prompt(target)
        if not target:
            await self._reply(
                event,
                "请指定模型序号或名称，例如：切换模型 1 / 切换模型 gpt-image-2\n"
                "发送「模型列表」可先查看当前协议支持的模型。\n"
                "💡 在末尾加「全局」可改所有人默认，例：切换模型 1 全局",
            )
            return
        protocol = _text_of(info.get("protocol")) or self.cfg.active_protocol()
        supplier = _text_of(info.get("supplier") or info.get("channel_name"))
        options = self._model_options(protocol, supplier)
        if not options:
            await self._reply(
                event,
                "📭 还没有可用的模型候选。\n"
                "请先发送「模型列表」联网获取一次，或直接在插件设置页填写模型名。",
            )
            return
        index, value = resolve_choice(target, options)
        if index < 0:
            # 允许直接指定候选列表之外的模型名（中转站模型名更新很快），
            # 只在明显不像模型名（含空格）时给出候选列表提示。
            if not re.fullmatch(r"[\w.\-/:+]{1,120}", target):
                await self._reply(
                    event,
                    "❌ 没找到模型「%s」。\n%s"
                    % (
                        target,
                        render_numbered_list(
                            "🧠 当前可用模型", options, _text_of(info.get("model"))
                        ),
                    ),
                )
                return
            if for_all:
                self.cfg.set_model_for(protocol, target)
                self.cfg.save()
                await self.state.set(umo, "model", "")
                await self._reply(
                    event,
                    f"✅ 已将全局模型切换为：{target}\n"
                    f"协议：{PROTOCOL_SHORT_LABELS.get(protocol, protocol)} · 所有会话都会使用该模型\n"
                    "ℹ️ 该模型不在候选列表内，已直接使用；若请求失败请发送「模型列表」查看可用模型。",
                )
                return
            await self.state.set(umo, "model", target)
            await self._reply(
                event,
                f"✅ 当前会话模型已切换为：{target}\n"
                f"协议：{PROTOCOL_SHORT_LABELS.get(protocol, protocol)} · 后续指令都会使用该模型\n"
                "ℹ️ 该模型不在候选列表内，已直接使用；若请求失败请发送「模型列表」查看可用模型。",
            )
            return
        if for_all:
            self.cfg.set_model_for(protocol, value)
            self.cfg.save()
            await self.state.set(umo, "model", "")
            await self._reply(
                event,
                f"✅ 已将全局模型切换为：{value}（序号 {index + 1}）\n"
                f"协议：{PROTOCOL_SHORT_LABELS.get(protocol, protocol)} · 所有会话都会使用该模型",
            )
            return
        await self.state.set(umo, "model", value)
        await self._reply(
            event,
            f"✅ 当前会话模型已切换为：{value}（序号 {index + 1}）\n"
            f"协议：{PROTOCOL_SHORT_LABELS.get(protocol, protocol)} · 后续指令都会使用该模型\n"
            "（仅本会话生效，加「全局」可改所有人默认；发送「重置设置」可恢复默认）",
        )

    async def _switch_size(
        self, event: AstrMessageEvent, parsed: ParsedCommand, info: dict, umo: str
    ) -> None:
        target, for_all = _split_global(parsed.arg)
        target = _clean_prompt(target)
        if not target:
            sizes = self.cfg.image_sizes()
            await self._reply(
                event,
                "请指定尺寸序号或尺寸值，例如：切换尺寸 3 / 切换尺寸 1536x1024\n"
                "💡 在末尾加「全局」可改所有人默认，例：切换尺寸 3 全局\n"
                + render_numbered_list(
                    "📐 可用尺寸", sizes, _text_of(info.get("size")) or self.cfg.active_size()
                ),
            )
            return
        sizes = self.cfg.image_sizes()
        index, value = resolve_choice(target, sizes)
        normalized = value if index >= 0 else normalize_size(target, "")
        if not normalized or not looks_like_size(normalized):
            await self._reply(
                event,
                f"❌ 尺寸格式不正确：{target}\n应形如 1024x1024 / 1536x1024 / auto，也可以直接发送序号。\n"
                + render_numbered_list(
                    "📐 可用尺寸", sizes, _text_of(info.get("size")) or self.cfg.active_size()
                ),
            )
            return
        if normalized not in self.cfg.image_sizes():
            self.cfg.add_size(normalized)
            self.cfg.save()
        hint = f"（序号 {index + 1}）" if index >= 0 else ""
        if for_all:
            self.cfg.set_active_size(normalized)
            self.cfg.save()
            await self.state.set(umo, "size", "")
            await self._reply(event, f"✅ 已将全局尺寸切换为：{normalized}{hint}（所有会话生效）")
            return
        await self.state.set(umo, "size", normalized)
        await self._reply(event, f"✅ 当前会话尺寸已切换为：{normalized}{hint}")

    async def _switch_protocol(
        self, event: AstrMessageEvent, parsed: ParsedCommand, info: dict, umo: str
    ) -> None:
        target, for_all = _split_global(parsed.arg)
        target = _clean_prompt(target)
        protocols = list(PROTOCOL_ORDER)
        labels = [PROTOCOL_SHORT_LABELS.get(key, key) for key in protocols]
        if not target:
            await self._reply(
                event,
                "请指定协议序号或名称，例如：切换协议 2 / 切换协议 Gemini\n"
                "💡 在末尾加「全局」可改所有人默认，例：切换协议 2 全局\n"
                + render_numbered_list(
                    "🔌 接口协议",
                    ["%s（%s）" % (label, key) for key, label in zip(protocols, labels)],
                    self._protocol_row(info),
                ),
            )
            return
        index, key = resolve_choice(target, protocols)
        if index < 0:
            index, key = resolve_choice(target, labels)
        if index < 0:
            await self._reply(
                event,
                f"❌ 没找到协议「{target}」。\n"
                + render_numbered_list(
                    "🔌 接口协议",
                    ["%s（%s）" % (label, key) for key, label in zip(protocols, labels)],
                    self._protocol_row(info),
                ),
            )
            return
        current_supplier = _text_of(info.get("supplier") or info.get("channel_name"))
        if for_all:
            self.cfg.set_active_protocol(key)
            self.cfg.save()
        await self.state.set(umo, "protocol", key)
        await self.state.set(umo, "model", "")
        model = self.cfg.model_for(key) or default_model_for(key)
        if for_all:
            await self._reply(
                event,
                f"✅ 已将全局协议切换为：{PROTOCOL_SHORT_LABELS.get(key, key)}（序号 {index + 1}）\n"
                f"供应商：{current_supplier or '—'} · 模型：{model}\n"
                "所有会话都会使用该协议；若模型不对，可发送「模型列表」后再用「切换模型」调整。",
            )
            return
        await self._reply(
            event,
            f"✅ 当前会话协议已切换为：{PROTOCOL_SHORT_LABELS.get(key, key)}（序号 {index + 1}）\n"
            f"供应商：{current_supplier or '—'} · 模型：{model}\n"
            "若该协议下模型不对，可发送「模型列表」后再用「切换模型」调整。",
        )

    async def _switch_supplier(
        self, event: AstrMessageEvent, parsed: ParsedCommand, info: dict, umo: str
    ) -> None:
        target, for_all = _split_global(parsed.arg)
        target = _clean_prompt(target)
        names = self.cfg.supplier_names()
        if not target:
            await self._reply(
                event,
                "请指定供应商序号或名称，例如：切换供应商 2\n"
                "💡 在末尾加「全局」可改所有人默认，例：切换供应商 2 全局\n"
                + render_numbered_list(
                    "🏢 供应商列表", names, _text_of(info.get("supplier") or info.get("channel_name"))
                ),
            )
            return
        index, value = resolve_choice(target, names)
        if index < 0:
            await self._reply(
                event,
                f"❌ 没找到供应商「{target}」。\n"
                + render_numbered_list(
                    "🏢 供应商列表", names, _text_of(info.get("supplier") or info.get("channel_name"))
                ),
            )
            return
        if for_all:
            self.cfg.set_active_supplier(value)
            self.cfg.save()
        await self.state.set(umo, "supplier", value)
        await self.state.set(umo, "model", "")
        protocol = _text_of(info.get("protocol")) or self.cfg.active_protocol()
        model = self.cfg.model_for(protocol) or default_model_for(protocol)
        prefix = "已将全局供应商切换为" if for_all else "当前会话供应商已切换为"
        await self._reply(
            event,
            f"✅ {prefix}：{value}（序号 {index + 1}）\n"
            f"协议：{PROTOCOL_SHORT_LABELS.get(protocol, protocol)} · 模型：{model}",
        )

    async def _add_size(
        self, event: AstrMessageEvent, parsed: ParsedCommand, info: dict
    ) -> None:
        target = _clean_prompt(parsed.arg)
        if not target:
            await self._reply(event, "请给出要添加的尺寸，例如：添加尺寸 2048x2048")
            return
        normalized = normalize_size(target, "")
        if not normalized or not looks_like_size(normalized):
            await self._reply(event, f"❌ 尺寸格式不正确：{target}")
            return
        if self.cfg.add_size(normalized):
            self.cfg.save()
            await self._reply(event, f"✅ 已添加尺寸：{normalized}")
        else:
            await self._reply(event, f"ℹ️ 尺寸已存在：{normalized}")

    # ------------------------------------------------- 供应商 / 统计 / 重置
    def _protocol_row(self, info: dict) -> str:
        key = _text_of(info.get("protocol")) or self.cfg.active_protocol()
        return "%s（%s）" % (PROTOCOL_SHORT_LABELS.get(key, key), key)

    def _model_options(self, protocol: str, supplier: str = "") -> list:
        """当前协议下的模型候选（配置候选 + 远端缓存），供序号切换使用。"""
        try:
            if supplier and callable(getattr(self.cfg, "model_cache_for_supplier", None)):
                values = [self.cfg.model_for(protocol), self.cfg.edit_model_for(protocol)]
                values.extend(self.cfg.model_cache_for_supplier(protocol, supplier) or [])
            else:
                values = list(self.cfg.models_for(protocol) or [])
        except Exception:  # noqa: BLE001
            values = []
        if not values:
            fallback = self.cfg.model_for(protocol) or default_model_for(protocol)
            if fallback:
                values = [fallback]
        result: list = []
        for item in values:
            text = _text_of(item).strip()
            if text and text not in result:
                result.append(text)
        return result

    async def _list_suppliers(self, event: AstrMessageEvent, info: dict) -> None:
        names = self.cfg.supplier_names()
        current = _text_of(info.get("supplier") or info.get("channel_name"))
        if not names:
            await self._reply(
                event,
                "📭 还没有配置供应商。\n请在 %s 中添加中转站地址与密钥。" % _SETTINGS_PATH,
            )
            return
        protocol = _text_of(info.get("protocol")) or self.cfg.active_protocol()
        lines = [
            render_numbered_list("🏢 供应商列表", names, current),
            "",
            "当前协议：%s（序号 %d/%d）"
            % (
                PROTOCOL_SHORT_LABELS.get(protocol, protocol),
                (list(PROTOCOL_ORDER).index(protocol) + 1) if protocol in PROTOCOL_ORDER else 0,
                len(PROTOCOL_ORDER),
            ),
            "发送「切换供应商 <序号或名称>」可切换。",
        ]
        await self._reply(event, "\n".join(lines))

    async def _show_stats(self, event: AstrMessageEvent) -> None:
        snapshot = self.stats.snapshot()
        totals = snapshot.get("totals") or {}
        lines = [
            "📊 运行统计",
            "━━━━━━━━━━━━━━━",
            "· 成功：%s 次" % totals.get("success", 0),
            "· 失败：%s 次" % totals.get("failed", 0),
            "· 部分失败：%s 次" % totals.get("partial", 0),
            "· 取消：%s 次" % totals.get("cancelled", 0),
            "· 出图：%s 张" % totals.get("images", 0),
            "· 重试：%s 次" % totals.get("retries", 0),
            "· 平均耗时：%s 秒" % totals.get("avg_seconds", 0),
            "· 统计起点：%s" % (snapshot.get("started_at") or "—"),
        ]
        last_time = totals.get("last_time")
        if last_time:
            lines.append("· 最近一次：%s" % last_time)
        last_error = totals.get("last_error")
        if last_error:
            lines.append("· 最近错误：%s" % last_error)
        recent = snapshot.get("recent") or []
        if recent:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("最近 %d 次：" % min(len(recent), 5))
            for item in recent[:5]:
                status = item.get("status") or ("success" if item.get("ok") else "failed")
                flag = {"success": "✅", "partial": "⚠️", "cancelled": "⏹️"}.get(status, "❌")
                lines.append(
                    "· %s %s %s 秒 · %s · 重试 %s"
                    % (
                        flag,
                        item.get("kind") or "draw",
                        item.get("seconds", 0),
                        item.get("model") or "—",
                        item.get("retries", 0),
                    )
                )
        lines.append("━━━━━━━━━━━━━━━")
        lines.append("发送「重置设置」可清空当前会话的模型 / 尺寸 / 协议覆盖。")
        await self._reply(event, "\n".join(lines))

    async def _reset_session(
        self, event: AstrMessageEvent, info: dict, umo: str
    ) -> None:
        overrides = {}
        try:
            overrides = await self.state.snapshot(umo)
        except Exception:  # noqa: BLE001
            overrides = {}
        if not overrides:
            await self._reply(event, "ℹ️ 当前会话没有自定义设置，无需重置。")
            return
        await self.state.clear(umo)
        changed = "、".join(
            "%s→%s" % (_OVERRIDE_LABELS.get(key, key), value)
            for key, value in overrides.items()
        )
        protocol = self.cfg.active_protocol()
        await self._reply(
            event,
            "♻️ 已重置当前会话设置\n"
            "被清除的覆盖项：%s\n"
            "现在统一使用全局配置：供应商 %s · 协议 %s · 模型 %s · 尺寸 %s"
            % (
                changed,
                self.cfg.active_supplier_name() or "—",
                PROTOCOL_SHORT_LABELS.get(protocol, protocol),
                self.cfg.model_for(protocol) or default_model_for(protocol),
                self.cfg.active_size(),
            ),
        )

    async def _del_size(
        self, event: AstrMessageEvent, parsed: ParsedCommand, info: dict
    ) -> None:
        target = _clean_prompt(parsed.arg)
        if not target:
            await self._reply(event, "请给出要删除的尺寸，例如：删除尺寸 2048x2048")
            return
        normalized = normalize_size(target, target)
        if self.cfg.remove_size(normalized):
            self.cfg.save()
            await self._reply(event, f"✅ 已删除尺寸：{normalized}")
        else:
            await self._reply(event, f"❌ 删除失败：尺寸不存在，或这是最后一个尺寸。")

    async def _toggle_group(self, event: AstrMessageEvent, enable: bool) -> None:
        try:
            group_id = _text_of(event.get_group_id())
        except Exception:  # noqa: BLE001
            group_id = ""
        if not group_id:
            await self._reply(event, "本指令需要在群聊中使用。")
            return
        changed = self.cfg.set_group_enabled(group_id, enable)
        if changed:
            self.cfg.save()
            action = "开启" if enable else "关闭"
            await self._reply(event, f"✅ 已{action}本群（{group_id}）的绘画功能。")
        else:
            state = "已开启" if enable else "已关闭"
            await self._reply(event, f"ℹ️ 本群（{group_id}）已经是{state}状态，无需重复操作。")

    async def _master_change(
        self, event: AstrMessageEvent, parsed: ParsedCommand, add: bool
    ) -> None:
        target = _clean_prompt(parsed.arg)
        if not target:
            try:
                target = _text_of(event.get_sender_id())
            except Exception:  # noqa: BLE001
                target = ""
        if not target:
            await self._reply(
                event,
                "请给出用户 ID，例如：添加主人 1234567\n（也可以 @ 对方后再发送本指令）",
            )
            return
        target = _MENTION_RE.sub(" ", target).strip()
        matched = re.search(r"(\d{5,20})", target)
        if matched:
            target = matched.group(1)
        if add:
            if self.cfg.add_master(target):
                self.cfg.save()
                await self._reply(event, f"✅ 已添加主人：{target}")
            else:
                await self._reply(event, f"ℹ️ 该用户已经是主人：{target}")
        else:
            if self.cfg.remove_master(target):
                self.cfg.save()
                await self._reply(event, f"✅ 已移除主人：{target}")
            else:
                await self._reply(event, f"❌ 该用户不在主人列表中：{target}")

    # ------------------------------------------- 预设套用 / 抽卡 / 排行 / 图生文
    async def _preset_apply(
        self, event: AstrMessageEvent, parsed: ParsedCommand, info: dict
    ) -> None:
        """需求 14：套用某条内置提示词出图，可追加补充要求。"""
        raw = _clean_prompt(parsed.arg)
        if not raw:
            await self._preset_list(event)
            return
        parts = re.split(r"[\s　]+", raw, maxsplit=1)
        key = parts[0].strip()
        extra = parts[1].strip() if len(parts) > 1 else ""
        entry = self.cfg.get_prompt(key)
        if entry is None:
            await self._reply(
                event,
                "❌ 没找到预设「%s」。\n%s"
                % (key, render_numbered_list("📚 可用预设", self.cfg.prompt_names())),
            )
            return
        body = render_preset(
            str(entry.get("text") or ""),
            用户=_text_of(event.get_sender_name()),
            补充要求=extra,
        )
        prompt = (body + "\n" + extra).strip() if extra and extra not in body else body
        await self._draw(
            event,
            ParsedCommand(kind=KIND_DRAW, arg=prompt, raw=parsed.raw, command="预设"),
        )

    async def _gacha(self, event: AstrMessageEvent, info: dict) -> None:
        """需求 8：每日一次盲盒抽卡（随机画风 + 随机主题）。"""
        if not self.cfg.gacha_enabled():
            await self._reply(event, "🎲 抽卡功能已在插件设置中关闭。")
            return
        uid = _text_of(event.get_sender_id())
        try:
            used = self.fun.gacha_used_today(uid)
        except Exception:  # noqa: BLE001
            used = False
        if used:
            await self._reply(
                event,
                "🎲 今天的盲盒已经抽过啦，明天再来～\n（发送「排行」看看大家画了多少）",
            )
            return
        try:
            style = pick_gacha_style(self.cfg.gacha_styles())
        except Exception:  # noqa: BLE001
            style = None
        if not style:
            await self._reply(event, "🎲 画风池是空的，请主人先在插件设置里补充。")
            return
        rarity = str(style.get("rarity") or "common")
        label = {"common": "普通", "rare": "稀有", "epic": "史诗"}.get(rarity, rarity)
        prompt = str(style.get("text") or "").strip()
        try:
            self.fun.mark_gacha(uid, str(style.get("name") or ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[gpt-image] 记录抽卡失败：{exc}")
        await self._reply(
            event,
            "🎲 今日盲盒：%s【%s】\n🎨 %s"
            % (style.get("name") or "神秘画风", label, prompt[:60]),
        )
        if prompt:
            await self._draw(
                event,
                ParsedCommand(kind=KIND_DRAW, arg=prompt, raw="抽卡", command="抽卡"),
            )

    async def _rank(self, event: AstrMessageEvent) -> None:
        """需求 9：绘画排行榜（本周 + 累计）。"""
        if not self.cfg.rank_enabled():
            await self._reply(event, "🏆 排行榜已在插件设置中关闭。")
            return
        try:
            weekly = self.fun.ranking("week", limit=10)
            total = self.fun.total_draws()
        except Exception as exc:  # noqa: BLE001
            await self._reply(event, "🏆 读取排行榜失败：%s" % exc)
            return
        lines = ["🏆 绘画排行榜（本周）", "━━━━━━━━━━━━━━━"]
        if weekly:
            medals = ["🥇", "🥈", "🥉"]
            for index, item in enumerate(weekly):
                prefix = medals[index] if index < len(medals) else "%d." % (index + 1)
                lines.append(
                    "%s %s　%s 张"
                    % (
                        prefix,
                        item.get("name") or item.get("uid") or "匿名",
                        item.get("count", 0),
                    )
                )
        else:
            lines.append("（本周还没有记录，发送「绘画 一只猫」抢占榜首）")
        lines.append("━━━━━━━━━━━━━━━")
        lines.append("累计出图：%s 张" % total)
        await self._reply(event, "\n".join(lines))

    async def _img2prompt(self, event: AstrMessageEvent, info: dict) -> None:
        """需求 11：图片转提示词（需要视觉模型）。"""
        if not self.cfg.img2prompt_enabled():
            await self._reply(
                event,
                "🔍 图片转提示词未开启。\n"
                "请在插件设置页打开「图片转提示词」并填写视觉模型（例如 gpt-4o）。",
            )
            return
        channel = info.get("channel")
        ready, reason = _channel_ready(channel)
        if not ready:
            await self._reply(event, reason)
            return
        try:
            images = await collect_input_images(event, self.cfg, include_mentions=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[gpt-image] 采集图片失败：{exc}")
            images = []
        if not images:
            await self._reply(
                event,
                "🔍 请附图后再发送「图片转提示词」（直接发送图片 / 引用图片 / 附带图片链接都可以）。",
            )
            return
        model = self.cfg.img2prompt_model() or self.cfg.translate_model()
        await self._reply(event, "🔍 正在分析图片，请稍候…")
        try:
            source = images[0]
            data = getattr(source, "data", "") or getattr(source, "b64", "")
            url = _text_of(getattr(source, "url", ""))
            text = await image_to_prompt(
                channel, model, data or url, self.cfg.timeout(), self.cfg.proxy()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[gpt-image] 图片转提示词失败：{exc}")
            text = ""
        if not text:
            await self._reply(event, "😕 反推失败，请检查视觉模型名称或稍后重试。")
            return
        await self._reply(event, "🔍 反推的提示词：\n━━━━━━━━━━━━━━━\n" + text)
