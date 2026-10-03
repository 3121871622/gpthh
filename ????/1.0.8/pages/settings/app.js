/**
 * gpt-image-2.5绘画 · 插件设置页（AstrBot Plugin Page）
 * ---------------------------------------------------------------------------
 * 用途：在 AstrBot WebUI 的插件 Page 中配置本插件：
 *       概览、供应商与协议、模型与尺寸、指令设置、文案设置、权限与范围、使用说明。
 * 约束：纯原生 ES module 前端，不使用任何外部 CDN / 字体 / 图片；所有请求经
 *       window.AstrBotPluginPage bridge 转发到插件后端 Web API。
 * 后端接口（不带插件名前缀，由 bridge 转发）：
 *       config / suppliers / suppliers/test / protocols / models / platforms /
 *       recent / status / stats / sizes / sizes/add / sizes/delete。
 * 主题：bridge 会维护 <html data-theme="light|dark">，样式见 ./style.css。
 */

const PLUGIN_TITLE = "gpt-image-2.5绘画";
const FALLBACK_VERSION = "1.0.8";

const THEME_STORAGE_KEY = "astrbot-gpt-image-settings-theme";
const THEME_OPTIONS = [
  { value: "light", label: "蓝白科技" },
  { value: "dark", label: "深色科技" },
  { value: "anime", label: "二次元蓝" },
  { value: "cyber", label: "赛博霓虹" },
  { value: "paper", label: "纸感工作台" },
];

/* ------------------------------------------------------------------ 常量 */

const PROTOCOL_KEYS = ["openai", "gemini", "grok", "flux", "sdwebui", "comfyui", "jimeng", "tongyi"];

const PROTOCOL_SHORT_LABELS = {
  openai: "OpenAI 兼容",
  gemini: "Gemini 兼容",
  grok: "Grok 兼容",
  flux: "Flux 中转",
  sdwebui: "SD WebUI",
  comfyui: "ComfyUI",
  jimeng: "即梦",
  tongyi: "通义万相",
};

/** 协议一句话请求形态，用于协议切换卡片。 */
const PROTOCOL_FORMS = {
  openai: "POST /images/generations · POST /images/edits",
  gemini: "POST /v1beta/models/<模型>:generateContent · inlineData",
  grok: "POST /images/generations · 失败回退 chat/completions",
  flux: "POST /images/generations · Flux 中转",
  sdwebui: "POST /sdapi/v1/txt2img · img2img",
  comfyui: "POST /prompt · 工作流出图",
  jimeng: "异步任务提交 · 图片 URL 返回",
  tongyi: "DashScope 异步任务 · 图片 URL 返回",
};

const PROTOCOL_FALLBACK_LABELS = {
  openai: "OpenAI 兼容（/images/generations · /images/edits）",
  gemini: "Gemini 兼容（generateContent · inlineData）",
  grok: "Grok 兼容（images + chat/completions 回退）",
  flux: "Flux 中转（OpenAI 风格）",
  sdwebui: "Stable Diffusion WebUI（txt2img / img2img）",
  comfyui: "ComfyUI（prompt 工作流）",
  jimeng: "即梦 / 火山引擎（异步任务）",
  tongyi: "通义万相 / DashScope（异步任务）",
};

const PROTOCOL_HINTS = {
  openai:
    "支持图片生成与编辑。中转站地址可只填域名（如 https://api.example.com），程序会自动补 /v1；已填版本路径也不会重复追加。",
  gemini:
    "请求形态：POST {base_url}/v1beta/models/<模型>:generateContent，图片以 inlineData 内联返回；模型列表读取 {base_url}/v1beta/models。",
  grok:
    "请求形态：默认走 OpenAI 兼容的 /images/generations 与 /images/edits；当接口不支持图片编辑时，自动回退到 chat/completions（图片以 data-url 传入）。",
  flux: "请求形态：OpenAI 风格的图片生成接口；中转站地址可只填域名，程序会自动补版本路径。",
  sdwebui: "请求形态：本地 SD WebUI 的 /sdapi/v1/txt2img 与 /sdapi/v1/img2img，不自动补 /v1。",
  comfyui: "请求形态：ComfyUI /prompt 工作流接口，不自动补 /v1。",
  jimeng: "请求形态：即梦异步任务接口，按供应商网关实际路径请求。",
  tongyi: "请求形态：DashScope 异步任务接口，按供应商网关实际路径请求。",
};

/** 协议图标（纯 emoji，避免引入任何外部图片资源）。 */
const PROTOCOL_ICONS = { openai: "🧩", gemini: "💎", grok: "⚡", flux: "🌊", sdwebui: "🧪", comfyui: "🧱", jimeng: "🌋", tongyi: "☁️" };

/** 各协议的强调色类名，用于让三种协议在界面上有区分度。 */
const PROTOCOL_TONES = { openai: "tone-openai", gemini: "tone-gemini", grok: "tone-grok", flux: "tone-flux", sdwebui: "tone-sdwebui", comfyui: "tone-comfyui", jimeng: "tone-jimeng", tongyi: "tone-tongyi" };

const DEFAULT_BASE_URLS = {
  openai: "https://api.openai.com/v1",
  gemini: "https://generativelanguage.googleapis.com",
  grok: "https://api.x.ai/v1",
  flux: "https://api.example.com/v1",
  sdwebui: "http://127.0.0.1:7860",
  comfyui: "http://127.0.0.1:8188",
  jimeng: "https://api.example.com",
  tongyi: "https://dashscope.aliyuncs.com/api/v1",
};

const DEFAULT_PROTOCOL_MODELS = {
  openai: { model: "gpt-image-2", edit_model: "" },
  gemini: { model: "gemini-2.5-flash-image", edit_model: "" },
  grok: { model: "grok-2-image", edit_model: "" },
  flux: { model: "flux-1.1-pro", edit_model: "" },
  sdwebui: { model: "", edit_model: "" },
  comfyui: { model: "", edit_model: "" },
  jimeng: { model: "jimeng-3.0", edit_model: "" },
  tongyi: { model: "wanx2.1-t2i-turbo", edit_model: "" },
};

const RELAY_PROTOCOLS = ["openai", "gemini", "grok", "flux"];
const SELFHOST_PROTOCOLS = ["sdwebui", "comfyui"];

const DEFAULT_COMMANDS = {
  draw: ["绘画", "画图"],
  edit: ["图片编辑", "编辑图片"],
  menu: ["绘画菜单", "菜单"],
  master: [
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
  ],
};

/** 供应商 / 协议 / 统计类指令：共 8 项，顺序固定，与后端逐一对应。 */
const SUPPLIER_COMMAND_FIELDS = [
  { label: "供应商列表", hint: "查看全部供应商（所有人可用）" },
  { label: "切换供应商", hint: "主人切换当前供应商，支持序号或名称" },
  { label: "协议列表", hint: "查看八种协议及当前协议" },
  { label: "切换协议", hint: "主人切换接口协议，支持序号或名称" },
  { label: "模型列表", hint: "查看当前协议的可用模型" },
  { label: "切换模型", hint: "主人切换模型，支持序号或名称" },
  { label: "运行统计", hint: "主人查看出图数量、成功率等统计" },
  { label: "重置设置", hint: "主人把当前会话的临时覆盖恢复为默认" },
];

const DEFAULT_SUPPLIER_COMMANDS = SUPPLIER_COMMAND_FIELDS.map((item) => item.label);

const DEFAULT_PROMPTS = {
  start_prompt: `🎨 收到，正在为你{prompt_type}…
🧠 模型：{模型}（{协议}）
📐 尺寸：{尺寸}
⏳ 最长等待 {超时} 秒，请稍候～`,
  done_prompt: `✅ {prompt_type}完成！
🖼 出图 {数量} 张
⏱ 耗时 {耗时} 秒
🧠 {模型} · {尺寸}`,
  menu_text: `🎨 gpt-image-2.5绘画 · 菜单
━━━━━━━━━━━━━━
🖌 文生图：{绘画指令} + 内容
   例：{绘画指令}广州塔宣传图
🖼 图片编辑：{编辑指令} + 内容 + 图片链接
📎 引用编辑：引用图片后发送 {编辑指令} + 内容
🧩 多图编辑：{编辑指令} + 内容 + 多个图片链接
🧑 头像编辑：{编辑指令}手办化@某人
━━━━━━━━━━━━━━
⚙️ 当前：{模型} · {尺寸} · {供应商}/{协议}
━━━━━━━━━━━━━━
📋 {菜单指令} 查看本菜单
🔌 {供应商菜单} 查看供应商列表
🧠 {模型菜单} 查看模型列表
🖼 {尺寸菜单} 查看尺寸列表
📐 {协议菜单} 查看接口协议
🆔 我的ID 查看我的用户ID / 群ID / 平台
━━━━━━━━━━━━━━
👑 主人指令：{主人指令}`,
};

const SIZE_PRESETS = [
  "auto",
  "1024x1024",
  "2048x2048",
  "4096x4096",
  "1536x1024",
  "1024x1536",
  "2048x1365",
  "1365x2048",
  "4096x2731",
  "2731x4096",
  "1280x720",
  "720x1280",
  "1024x683",
  "683x1024",
  "1024x1365",
  "1365x1024",
  "1152x2048",
  "2048x1152",
  "2560x1080",
  "1080x2560",
];

const PROMPT_VARIABLES = [
  { name: "{模型}", desc: "本次出图使用的模型名" },
  { name: "{尺寸}", desc: "本次出图使用的尺寸" },
  { name: "{供应商}", desc: "当前供应商名称" },
  { name: "{供应商序号}", desc: "当前供应商在供应商列表中的序号（1 起）" },
  { name: "{协议}", desc: "当前接口协议（OpenAI / Gemini / Grok）" },
  { name: "{协议序号}", desc: "当前协议在八种协议中的序号（1 起）" },
  { name: "{模型序号}", desc: "当前模型在模型列表中的序号（取不到时为空）" },
  { name: "{提示词}", desc: "用户输入的提示词内容" },
  { name: "{耗时}", desc: "本次耗时，保留两位小数（单位：秒）" },
  { name: "{用户}", desc: "触发用户的昵称" },
  { name: "{用户ID}", desc: "触发用户的 ID" },
  { name: "{时间}", desc: "当前时间（HH:MM:SS）" },
  { name: "{日期}", desc: "当前日期（YYYY-MM-DD）" },
  { name: "{数量}", desc: "本次生成的图片数量" },
  { name: "{当前模型}", desc: "当前配置的模型（菜单用）" },
  { name: "{当前尺寸}", desc: "当前配置的尺寸（菜单用）" },
  { name: "{绘画指令}", desc: "绘画指令列表的第一条" },
  { name: "{编辑指令}", desc: "图片编辑指令列表的第一条" },
  { name: "{菜单指令}", desc: "菜单指令列表的第一条" },
  { name: "{主人指令}", desc: "主人指令列表（顿号分隔）" },
];

const EXTRA_PROMPT_VARIABLES = [
  { name: "{触发方式}", desc: "当前触发方式（需要 @机器人 / 不需要 @）" },
  { name: "{群状态}", desc: "当前群聊策略（全部 / 白名单 / 黑名单）" },
  { name: "{群ID}", desc: "当前消息所在群的 ID，私聊时为空" },
  { name: "{平台}", desc: "当前消息来源的平台类型" },
  { name: "{机器人实例ID}", desc: "处理当前消息的机器人实例 ID" },
  { name: "{原始消息}", desc: "平台收到的原始消息内容" },
  { name: "{原始提示词}", desc: "用户提交的原始提示词" },
  { name: "{最终提示词}", desc: "经过处理后提交给模型的最终提示词" },
  { name: "{宽度}", desc: "本次出图尺寸的宽度" },
  { name: "{高度}", desc: "本次出图尺寸的高度" },
  { name: "{模式}", desc: "本次请求模式，例如绘画或图片编辑" },
  { name: "{批量数量}", desc: "本次批量生成的数量" },
  { name: "{冷却}", desc: "同用户冷却秒数，0 表示不限制" },
  { name: "{超时}", desc: "接口超时时间（秒）" },
  { name: "{prompt_type}", desc: "自动区分「绘画」或「图片编辑」" },
  { name: "{菜单}", desc: "完整菜单文本" },
  { name: "{模型菜单}", desc: "模型列表类指令的第一条" },
  { name: "{尺寸菜单}", desc: "尺寸列表类指令的第一条" },
  { name: "{协议菜单}", desc: "协议列表类指令的第一条" },
  { name: "{供应商菜单}", desc: "供应商列表类指令的第一条" },
  { name: "{日期时间}", desc: "日期与时间合并值（YYYY-MM-DD HH:MM:SS）" },
  { name: "{批量位置}", desc: "当前批量请求的位置（也可使用 {batch_index}）" },
  { name: "{画面比例}", desc: "从宽高约分得到的比例，例如 16:9" },
  { name: "{像素数}", desc: "从宽度和高度计算得到的总像素数" },
  { name: "{是否正方形}", desc: "宽高相等时显示“是”，否则显示“否”" },
  { name: "{是否批量}", desc: "请求数量大于 1 时显示“是”，否则显示“否”" },
  { name: "{是否有成功}", desc: "已有成功结果时显示“是”，否则显示“否”" },
  { name: "{是否有失败}", desc: "已有失败结果时显示“是”，否则显示“否”" },
];

const PLATFORM_PRESETS = [
  { id: "aiocqhttp", name: "OneBot v11（aiocqhttp）", desc: "通过 OneBot v11 协议接入 QQ" },
  { id: "qq_official", name: "QQ 官方机器人 · WebSocket", desc: "QQ 开放平台 WebSocket 长连接" },
  { id: "qq_official_webhook", name: "QQ 官方机器人 · Webhook", desc: "QQ 开放平台 Webhook 回调" },
];

const GROUP_MODE_OPTIONS = [
  { value: "all", label: "全部群聊生效（all）", desc: "不限制群号，所有群都可以绘画。" },
  { value: "whitelist", label: "仅白名单群生效（whitelist）", desc: "只有群列表中的群可以使用。" },
  { value: "blacklist", label: "黑名单群禁用（blacklist）", desc: "群列表中的群会被禁用，其余群可用。" },
];

const TRIGGER_MODE_OPTIONS = [
  {
    value: "at",
    title: "需要 @机器人 / 唤醒前缀（默认，推荐）",
    desc: "只有在群里 @机器人，或使用了 AstrBot 的唤醒前缀时才会响应，最稳妥。",
    badge: "推荐",
  },
  {
    value: "command",
    title: "不需要 @，消息以指令开头即响应（仅群聊）",
    desc: "适合 QQ 官方机器人等无法稳定 @ 的场景；群里任何人发「绘画 xxx」都会触发。",
    warn: "群里任何人发「绘画 xxx」都会触发，建议配合群白名单使用。",
  },
];

/* -------------------------------------------------------------- DOM 工具 */

const bridge = window.AstrBotPluginPage || null;

function h(tag, attrs, children) {
  const node = document.createElement(tag);
  const options = attrs || {};
  Object.keys(options).forEach((key) => {
    const value = options[key];
    if (value === null || value === undefined || value === false) return;
    if (key === "class") {
      node.className = String(value);
    } else if (key === "text") {
      node.textContent = String(value);
    } else if (key === "value") {
      node.value = String(value);
    } else if (key === "checked" || key === "selected" || key === "disabled" || key === "hidden" || key === "readOnly") {
      if (value) node.setAttribute(key === "readOnly" ? "readonly" : key, "");
      else node.removeAttribute(key === "readOnly" ? "readonly" : key);
    } else if (key === "dataset" && value && typeof value === "object") {
      Object.keys(value).forEach((dataKey) => {
        node.dataset[dataKey] = String(value[dataKey]);
      });
    } else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else {
      node.setAttribute(key, String(value));
    }
  });
  appendChildren(node, children);
  return node;
}

function appendChildren(node, children) {
  if (children === null || children === undefined || children === false) return;
  if (Array.isArray(children)) {
    children.forEach((child) => appendChildren(node, child));
    return;
  }
  if (children instanceof Node) {
    node.appendChild(children);
    return;
  }
  node.appendChild(document.createTextNode(String(children)));
}

function clear(node) {
  while (node && node.firstChild) node.removeChild(node.firstChild);
}

/** 创建状态胶囊（.pill），kind 取 ok / warn / err / plain / info。 */
function pill(text, kind, title) {
  return h("span", {
    class: "pill " + (kind || "plain"),
    text: String(text === undefined || text === null ? "" : text),
    title: title || null,
  });
}

/** 创建圆角方块图标底座（.icon-tile）。 */
function iconTile(icon, size) {
  return h("span", {
    class: "icon-tile" + (size ? " " + size : ""),
    "aria-hidden": "true",
    text: String(icon || "🎨"),
  });
}

/** 从 URL 里取出 host，取不到返回空串（用于供应商卡片上的域名胶囊）。 */
function hostOf(url) {
  const text = String(url || "").trim();
  if (!text) return "";
  const matched = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\/([^/?#]+)/.exec(text);
  return matched ? matched[1] : "";
}

/** 简化的耗时展示（毫秒 -> 秒，保留两位小数）。 */
function secondsText(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return "—";
  return (number / 1000).toFixed(2) + " 秒";
}

function $(id) {
  return document.getElementById(id);
}

/** 创建一段说明文本（hint），空内容返回 null 便于在数组里自动跳过。 */
function hint(text, extraClass) {
  if (!text) return null;
  return h("div", { class: "hint" + (extraClass ? " " + extraClass : ""), text: String(text) });
}

/** 创建一个状态徽标。 */
function badge(text, kind) {
  if (!text) return null;
  return h("span", { class: "badge" + (kind ? " " + kind : ""), text: String(text) });
}

/** 空状态引导块：图标 + 标题 + 说明 + 可选操作按钮。 */
function emptyState(icon, title, text, actions) {
  return h("div", { class: "empty" }, [
    h("div", { class: "empty-icon", text: icon || "📭" }),
    h("div", { class: "empty-title", text: title || "暂无内容" }),
    text ? h("div", { class: "empty-text", text: text }) : null,
    actions && actions.length ? h("div", { class: "empty-actions" }, actions) : null,
  ]);
}

function toArray(value) {
  if (Array.isArray(value)) return value;
  if (value === null || value === undefined || value === "") return [];
  return [value];
}

function errText(error) {
  if (!error) return "未知错误";
  if (typeof error === "string") return error;
  if (error.message) return String(error.message);
  return String(error);
}

function trimList(list) {
  return toArray(list)
    .map((item) => String(item === null || item === undefined ? "" : item).trim())
    .filter(Boolean);
}

function num(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function clampInt(value, min, max, fallback) {
  const parsed = Math.round(num(value, fallback));
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, parsed));
}

function looksLikeSize(value) {
  const text = String(value || "").trim().toLowerCase();
  if (!text) return false;
  if (text === "auto") return true;
  return /^\d{2,5}x\d{2,5}$/.test(text);
}

/** 判断 URL 是否以允许的协议开头（http / https / socks5）。 */
function looksLikeUrl(value, allowSocks) {
  const text = String(value || "").trim();
  if (!text) return false;
  if (allowSocks) return /^(https?|socks5):\/\//i.test(text);
  return /^https?:\/\//i.test(text);
}

/* ---------------------------------------------------------------- 状态 */

const state = {
  loaded: false,
  saving: false,
  dirty: false,
  version: FALLBACK_VERSION,
  protocolLabels: Object.assign({}, PROTOCOL_FALLBACK_LABELS),
  protocolOrder: PROTOCOL_KEYS.slice(),
  config: null,
  status: null,
  stats: null,
  recent: null,
  suppliers: [],
  protocolModels: {
    openai: { model: "", edit_model: "" },
    gemini: { model: "", edit_model: "" },
    grok: { model: "", edit_model: "" },
  },
  activeProtocol: "openai",
  modelCache: Object.create(null),
  modelMeta: Object.create(null),
  smartModelCache: Object.create(null),
  supplierDiagnostics: Object.create(null),
  completion: null,
  modelRequestSeq: 0,
  smartModelRefreshers: [],
  activeSectionId: "section-overview",
  platformTypes: [],
  platformInstances: [],
  lastError: "",
  sizeResult: null,
  commandEditors: null,
  promptTextareas: null,
  accessEditors: null,
  modelEditors: null,
};

let toastTimer = null;

/* ------------------------------------------------------------ API 封装 */

function unwrap(res) {
  const root = res;
  let value = res;
  let depth = 0;
  while (value && typeof value === "object" && "data" in value && value.data !== undefined && depth < 4) {
    value = value.data;
    depth += 1;
  }
  if (root && typeof root === "object" && root.ok === false) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      return {
        ...value,
        ok: false,
        message: value.message || root.message || "接口返回失败",
        error: value.error || root.error,
      };
    }
    return {
      ok: false,
      data: value,
      message: root.message || "接口返回失败",
      error: root.error,
    };
  }
  return value;
}

async function apiGet(endpoint, params) {
  if (!bridge) throw new Error("bridge 未注入：请从 AstrBot 插件页面打开本页");
  const res = await bridge.apiGet(endpoint, params || {});
  if (endpoint === "models") return readModelsResponse(res);
  return unwrap(res);
}

function modelNameFromValue(value) {
  if (typeof value === "string") return value.trim();
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  for (const key of ["id", "name", "model", "model_id", "modelId", "model_name", "key"]) {
    if (typeof value[key] === "string" && value[key].trim()) return value[key].trim();
  }
  return "";
}

function collectModelNames(value, output, seen, depth) {
  if (depth > 6 || value === null || value === undefined) return;
  if (typeof value === "string") {
    const name = value.trim();
    if (name) output.push(name);
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((item) => collectModelNames(item, output, seen, depth + 1));
    return;
  }
  if (typeof value !== "object") return;
  if (seen.has(value)) return;
  seen.add(value);
  let nested = false;
  for (const key of ["models", "data", "items", "results", "list", "objects", "model_list"]) {
    if (value[key] !== undefined) {
      nested = true;
      collectModelNames(value[key], output, seen, depth + 1);
    }
  }
  if (!nested) {
    const directName = modelNameFromValue(value);
    if (directName) output.push(directName);
  }
}

function readModelsResponse(response) {
  const layers = [];
  let current = response;
  for (let depth = 0; depth < 5 && current !== null && current !== undefined; depth += 1) {
    layers.push(current);
    if (typeof current !== "object" || Array.isArray(current) || !Object.prototype.hasOwnProperty.call(current, "data")) break;
    current = current.data;
  }
  const values = [];
  const seen = new Set();
  layers.forEach((layer) => collectModelNames(layer, values, seen, 0));
  const models = Array.from(new Set(values.map((item) => String(item).trim()).filter(Boolean)));
  const failed = layers.some((layer) => {
    if (!layer || typeof layer !== "object") return false;
    const status = Number(layer.status || layer.status_code || layer.statusCode);
    return layer.ok === false || (Number.isFinite(status) && status >= 400) || Boolean(layer.error && !models.length);
  });
  const message = layers.map((layer) => {
    if (!layer || typeof layer !== "object") return "";
    if (typeof layer.message === "string" && layer.message.trim()) return layer.message.trim();
    if (typeof layer.error === "string" && layer.error.trim()) return layer.error.trim();
    if (layer.error && typeof layer.error.message === "string") return layer.error.message.trim();
    return "";
  }).find(Boolean) || (failed ? "模型接口返回失败" : models.length ? "" : "接口没有返回任何模型");
  const metadataLayer = layers.find((layer) => layer && typeof layer === "object") || {};
  const total = layers.map((layer) => layer && Number(layer.total || layer.total_count || layer.count))
    .find((value) => Number.isFinite(value));
  const fetchedAt = layers.map((layer) => layer && (layer.fetched_at || layer.fetchedAt || layer.updated_at || layer.timestamp))
    .find((value) => value);
  const cached = layers.map((layer) => layer && (layer.cached === true || layer.from_cache === true || layer.fromCache === true))
    .find((value) => value === true) === true;
  return {
    ok: !failed && models.length > 0,
    models,
    message,
    total: Number.isFinite(total) ? total : models.length,
    cached,
    fetchedAt: fetchedAt || "",
    hasNext: metadataLayer.has_next === true || metadataLayer.hasNext === true,
  };
}

function supplierListFromValue(value) {
  if (Array.isArray(value)) return value;
  if (!value || typeof value !== "object") return value ? [value] : [];
  for (const key of ["suppliers", "providers", "channels", "items", "data"]) {
    if (value[key] !== undefined) return supplierListFromValue(value[key]);
  }
  return Object.entries(value).map(([name, item]) => {
    if (item && typeof item === "object" && !Array.isArray(item)) return { ...item, name: item.name || name };
    return { name, base_url: item };
  });
}

function normalizeSupplier(item) {
  if (typeof item === "string") return { name: item.trim(), base_url: "", api_key: "", api_key_set: false, api_key_masked: "" };
  const source = item && typeof item === "object" ? item : {};
  const apiKey = source.api_key || source.apiKey || source.key || "";
  const name = source.name || source.title || source.id || source.provider || source.channel || "";
  const normalized = {
    ...source,
    name: String(name).trim(),
    base_url: String(source.base_url || source.baseUrl || source.url || source.endpoint || "").trim(),
    api_key: "",
    api_key_set: Boolean(source.api_key_set || source.apiKeySet || source.has_api_key || apiKey),
    api_key_masked: String(source.api_key_masked || source.apiKeyMasked || source.masked_api_key || ""),
    model: String(source.model || ""),
    edit_model: String(source.edit_model || source.editModel || ""),
  };
  ["apiKey", "key", "secret", "token", "password", "api_key_plain"].forEach((key) => delete normalized[key]);
  return normalized;
}

function loadModelCache(config, activeSupplier) {
  state.modelCache = Object.create(null);
  state.modelMeta = Object.create(null);
  const rawCache = config && config.model_cache;
  if (!rawCache || typeof rawCache !== "object") return;
  Object.entries(rawCache).forEach(([key, value]) => {
    const parsedResponse = readModelsResponse(value);
    const models = parsedResponse.models;
    if (!models.length) return;
    let supplier = activeSupplier;
    let protocol = key;
    try {
      const parsed = JSON.parse(key);
      if (Array.isArray(parsed)) {
        supplier = String(parsed[0] || supplier);
        protocol = String(parsed[1] || protocol);
      }
    } catch (error) {
      const parts = key.split("::");
      if (parts.length === 2 && PROTOCOL_KEYS.includes(parts[1])) {
        supplier = parts[0];
        protocol = parts[1];
      }
    }
    if (PROTOCOL_KEYS.includes(protocol)) {
      const cacheKey = modelCacheKey(supplier, protocol);
      state.modelCache[cacheKey] = models;
      state.modelMeta[cacheKey] = {
        total: parsedResponse.total || models.length,
        cached: true,
        fetchedAt: parsedResponse.fetchedAt || "",
        source: "配置缓存",
      };
    }
  });
}

function modelMetaFor(supplier, protocol) {
  const key = modelCacheKey(supplier, protocol);
  return state.modelMeta[key] || null;
}

function rememberModels(supplier, protocol, result, source) {
  const key = modelCacheKey(supplier, protocol);
  const models = trimList(result && result.models);
  if (!models.length) return;
  state.modelCache[key] = models;
  state.modelMeta[key] = {
    total: Number(result.total) || models.length,
    cached: Boolean(result.cached),
    fetchedAt: result.fetchedAt || new Date().toISOString(),
    source: source || (result.cached ? "服务端缓存" : "在线接口"),
  };
  updateCompletion();
}

function formatModelFetchedAt(value) {
  if (!value) return "刚刚";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

async function apiPost(endpoint, body) {
  if (!bridge) throw new Error("bridge 未注入：请从 AstrBot 插件页面打开本页");
  const res = await bridge.apiPost(endpoint, body || {});
  return unwrap(res);
}

function requireApiSuccess(value, fallbackMessage) {
  if (value && value.ok === false) {
    throw new Error(String(value.message || fallbackMessage || "接口返回失败"));
  }
  return value;
}

/* ---------------------------------------------------------------- Toast */

function toast(message, kind) {
  const host = $("toast-host");
  if (!host) return;
  const node = h("div", { class: "toast " + (kind || "info"), text: message });
  host.appendChild(node);
  window.setTimeout(() => {
    if (node.parentNode) node.parentNode.removeChild(node);
  }, 4200);
  if (toastTimer) window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    while (host.childNodes.length > 6) host.removeChild(host.firstChild);
  }, 500);
}

function applyTheme(theme, persist) {
  const value = THEME_OPTIONS.some((item) => item.value === theme) ? theme : "light";
  document.documentElement.setAttribute("data-theme", value);
  document.documentElement.setAttribute("data-settings-theme", value);
  const select = $("settings-theme");
  if (select) select.value = value;
  const radioMap = { light: "theme-blue", dark: "theme-dark", anime: "theme-anime", cyber: "theme-cyber", paper: "theme-paper" };
  Object.keys(radioMap).forEach((key) => {
    const radio = $(radioMap[key]);
    if (radio) radio.checked = key === value;
  });
  if (persist !== false) {
    try { window.localStorage.setItem(THEME_STORAGE_KEY, value); } catch (error) { /* storage may be unavailable */ }
  }
  if (value === "anime" && !$("anime-theme-style")) {
    const style = h("style", { id: "anime-theme-style", text: `
      html[data-theme="anime"] { --bg:#eef7ff; --bg-soft:#dff0ff; --panel:#ffffff; --panel-2:#f5fbff; --panel-3:#e5f4ff; --border:#c8e5ff; --border-strong:#9bcdf2; --text:#24324a; --text-strong:#12223d; --muted:#66809e; --accent:#2678d8; --accent-strong:#145cb2; --accent-weak:rgba(38,120,216,.16); --accent-softer:rgba(38,120,216,.08); --accent-gradient:linear-gradient(135deg,#49a7ff 0%,#6b7cff 50%,#c779ff 100%); --ok:#087b62; --warn:#9a6500; --err:#c73557; --info:#246bc2; --shadow:0 8px 26px rgba(42,112,184,.14); }
      html[data-theme="anime"] body { background-image: radial-gradient(circle at 12% 8%,rgba(119,208,255,.2),transparent 26%), radial-gradient(circle at 90% 4%,rgba(214,158,255,.18),transparent 25%); }
      html[data-theme="anime"] .brand-icon, html[data-theme="anime"] .section-no { text-shadow:0 0 16px rgba(38,120,216,.35); }
    ` });
    document.head.appendChild(style);
  }
}

function bindThemeSwitcher() {
  let select = $("settings-theme");
  const radios = ["theme-blue", "theme-dark", "theme-anime", "theme-cyber", "theme-paper"]
    .map((id) => $(id))
    .filter(Boolean);
  radios.forEach((radio) => {
    radio.addEventListener("change", (event) => {
      if (!event.currentTarget.checked) return;
      const theme = { "theme-blue": "light", "theme-dark": "dark", "theme-anime": "anime", "theme-cyber": "cyber", "theme-paper": "paper" }[event.currentTarget.id] || "light";
      applyTheme(theme);
      toast("已切换主题：" + (THEME_OPTIONS.find((item) => item.value === theme) || {}).label, "info");
    });
  });
  const actions = document.querySelector(".topbar-actions");
  if (!select && !radios.length && actions) {
    select = h("select", {
      id: "settings-theme",
      class: "select",
      title: "选择设置页面主题",
      "aria-label": "设置页面主题",
      onchange: (event) => {
        applyTheme(event.currentTarget.value);
        toast("已切换主题：" + (THEME_OPTIONS.find((item) => item.value === event.currentTarget.value) || {}).label, "info");
      },
    }, THEME_OPTIONS.map((item) => h("option", { value: item.value, text: item.label })));
    actions.insertBefore(select, actions.firstChild);
  }
  let stored = "light";
  try { stored = window.localStorage.getItem(THEME_STORAGE_KEY) || "light"; } catch (error) { /* use default */ }
  applyTheme(stored, false);
}

/* --------------------------------------------------------- 二次确认弹窗 */

/**
 * 危险操作二次确认。确认后 resolve(true)，取消 / 点遮罩 / 按 Esc 时 resolve(false)。
 *
 * :param options.title: 标题。
 * :param options.message: 正文说明。
 * :param options.danger: 正文中需要强调的文本（红色显示）。
 * :param options.confirmText: 确认按钮文本。
 */
function confirmDialog(options) {
  const opts = options || {};
  return new Promise((resolve) => {
    const host = $("modal-host");
    if (!host) {
      resolve(window.confirm(opts.message || "确认执行该操作？"));
      return;
    }

    let done = false;
    const finish = (value) => {
      if (done) return;
      done = true;
      document.removeEventListener("keydown", onKey);
      if (mask.parentNode) mask.parentNode.removeChild(mask);
      resolve(value);
    };

    const onKey = (event) => {
      if (event.key === "Escape") finish(false);
    };

    const confirmBtn = h("button", {
      class: opts.confirmKind === "primary" ? "btn primary" : "btn danger",
      type: "button",
      text: opts.confirmText || "确认",
      onclick: () => finish(true),
    });

    const mask = h("div", {
      class: "modal-mask",
      onclick: (event) => {
        if (event.target === mask) finish(false);
      },
    }, [
      h("div", { class: "modal", role: "dialog", "aria-modal": "true" }, [
        h("div", { class: "modal-head", text: opts.title || "请确认" }),
        h("div", { class: "modal-body" }, [
          h("div", { text: opts.message || "" }),
          opts.danger ? h("div", { class: "danger-text", text: opts.danger }) : null,
        ]),
        h("div", { class: "modal-foot" }, [
          h("button", { class: "btn", type: "button", text: "取消", onclick: () => finish(false) }),
          confirmBtn,
        ]),
      ]),
    ]);

    document.addEventListener("keydown", onKey);
    host.appendChild(mask);
    confirmBtn.focus();
  });
}

/* ------------------------------------------------------------ 顶部状态 */

function showError(message) {
  state.lastError = String(message || "未知错误");
  const banner = $("error-banner");
  const text = $("error-banner-text");
  if (text) text.textContent = state.lastError;
  if (banner) {
    banner.hidden = false;
    banner.setAttribute("role", "alert");
    banner.setAttribute("aria-live", "assertive");
    banner.setAttribute("aria-atomic", "true");
  }
}

function clearError() {
  state.lastError = "";
  const banner = $("error-banner");
  if (banner) banner.hidden = true;
}

function showOkBanner(message) {
  const banner = $("ok-banner");
  const text = $("ok-banner-text");
  if (!banner || !text) return;
  text.textContent = message;
  banner.setAttribute("role", "status");
  banner.setAttribute("aria-live", "polite");
  banner.setAttribute("aria-atomic", "true");
  banner.hidden = false;
  window.setTimeout(() => {
    banner.hidden = true;
  }, 6000);
}

function markDirty() {
  state.dirty = true;
  const flag = $("dirty-flag");
  if (flag) flag.hidden = false;
  updateCompletion();
}

function markClean() {
  state.dirty = false;
  const flag = $("dirty-flag");
  if (flag) flag.hidden = true;
  updateCompletion();
}

function setBusy(button, busy, busyText) {
  if (!button) return;
  if (busy) {
    button.dataset.label = button.dataset.label || button.textContent;
    button.disabled = true;
    button.classList.add("busy");
    button.setAttribute("aria-busy", "true");
    button.textContent = busyText || "处理中…";
  } else {
    button.disabled = false;
    button.classList.remove("busy");
    button.removeAttribute("aria-busy");
    if (button.dataset.label) button.textContent = button.dataset.label;
  }
}

function setResult(node, message, kind) {
  if (!node) return;
  const text = message || "";
  node.textContent = text;
  node.className = "result " + (kind || "info");
  node.setAttribute("role", "status");
  if (text) node.setAttribute("aria-live", "polite");
  else node.removeAttribute("aria-live");
}

function insertAtCaret(input, value) {
  if (!input) return;
  const text = String(value || "");
  const start = Number.isFinite(input.selectionStart) ? input.selectionStart : input.value.length;
  const end = Number.isFinite(input.selectionEnd) ? input.selectionEnd : start;
  input.value = input.value.slice(0, start) + text + input.value.slice(end);
  const caret = start + text.length;
  input.focus();
  try { input.setSelectionRange(caret, caret); } catch (error) { /* non-text controls */ }
}

/* ------------------------------------------------------- 标签式多值编辑 */

/**
 * 创建一个「标签式多值编辑器」。
 * 回车 / 逗号（含中文逗号）添加，Backspace 删除最后一个，点 × 删除。
 */
function createTagEditor(options) {
  const opts = options || {};
  const values = trimList(opts.values);
  const placeholder = opts.placeholder || "输入后回车添加，支持逗号批量";
  const onChange = typeof opts.onChange === "function" ? opts.onChange : null;
  const onRemove = typeof opts.onRemove === "function" ? opts.onRemove : null;
  const normalize = typeof opts.normalize === "function" ? opts.normalize : (v) => v;
  /* 内置默认值：命中的标签会打上「默认」标记，删除时需要二次确认。 */
  const defaults = trimList(opts.defaults);

  const chips = h("div", { class: "chips" });
  const input = h("input", {
    class: "tag-input",
    type: "text",
    placeholder,
    onkeydown: (event) => {
      if (event.key === "Enter" || event.key === "," || event.key === "，" || event.key === ";") {
        event.preventDefault();
        commit(input.value);
        input.value = "";
      } else if (event.key === "Backspace" && !input.value && values.length) {
        values.pop();
        emit();
      }
    },
    onpaste: (event) => {
      const text = event.clipboardData ? event.clipboardData.getData("text") : "";
      if (!text || (!text.includes(",") && !text.includes("，") && !text.includes("\n"))) return;
      event.preventDefault();
      commit(text.replace(/\n/g, ","));
    },
    onblur: () => {
      if (input.value.trim()) {
        commit(input.value);
        input.value = "";
      }
    },
  });

  const editor = h("div", { class: "tag-editor" }, [chips, input]);

  function emit() {
    render();
    if (onChange) onChange(values.slice());
  }

  function add(raw) {
    const text = normalize(String(raw).trim());
    if (!text) return false;
    if (values.indexOf(text) !== -1) return false;
    values.push(text);
    return true;
  }

  function commit(raw) {
    const parts = String(raw)
      .split(/[,，;\n]/)
      .map((item) => item.trim())
      .filter(Boolean);
    let added = false;
    parts.forEach((part) => {
      if (add(part)) added = true;
    });
    if (added) emit();
    else if (parts.length === 0) input.value = "";
    return added;
  }

  function removeAt(index) {
    const removed = values.slice(index, index + 1)[0];
    const isDefault = !!removed && defaults.indexOf(removed) !== -1;
    const drop = () => {
      values.splice(index, 1);
      render();
      if (onRemove) onRemove(removed, values.slice());
      else if (onChange) onChange(values.slice());
    };
    if (!isDefault) {
      drop();
      return;
    }
    /* 删除的是插件内置默认指令，先让用户确认一次，避免误删后以为功能坏了。 */
    confirmDialog({
      title: "删除默认指令",
      message: "「" + removed + "」是插件内置的默认指令。",
      danger: "删除后该指令将不再生效；如只是不想用，也可以保留它。",
      confirmText: "仍然删除",
    }).then((ok) => {
      if (ok) drop();
    });
  }

  function render() {
    clear(chips);
    values.forEach((value, index) => {
      const isDefault = defaults.indexOf(value) !== -1;
      chips.appendChild(
        h("span", {
          class: "chip" + (isDefault ? " default" : ""),
          title: isDefault ? "插件内置默认指令（删除前会二次确认）" : "自定义指令",
        }, [
          isDefault ? h("span", { class: "chip-dot", "aria-hidden": "true", text: "•" }) : null,
          h("span", { text: value }),
          h("button", {
            class: "chip-x",
            type: "button",
            title: isDefault ? "删除默认指令（需确认）" : "删除",
            "aria-label": "删除指令 " + value,
            text: "×",
            onclick: () => removeAt(index),
          }),
        ]),
      );
    });
  }

  render();
  editor.addEventListener("click", () => input.focus());

  return {
    element: editor,
    getValues: () => values.slice(),
    setValues: (next) => {
      values.length = 0;
      trimList(next).forEach((item) => add(item));
      render();
    },
    add,
    emit,
    commit,
  };
}

/* --------------------------------------------- 一键获取最近用户 / 最近群 */

/**
 * 把 recent 接口返回的一条记录整理成「平台类型 · 机器人实例 · 时间」说明文本。
 */
function recentMeta(item) {
  const parts = [];
  const platform = String((item && item.platform) || "").trim();
  const platformId = String((item && item.platform_id) || "").trim();
  const time = String((item && item.time) || "").trim();
  if (platform) parts.push(platform);
  if (platformId) parts.push("实例 " + platformId);
  if (time) parts.push(time);
  return parts.join(" · ");
}

/**
 * 创建「一键获取」候选列表（最近交互过的用户 / 群）。
 *
 * :param options.idLabel: 详情行前缀，例如「用户 ID」「群 ID」。
 * :param options.titleOf: 由记录生成标题文本，缺省时直接用 ID。
 * :param options.emptyText: 没有记录时展示的提示文案。
 * :param options.commit: 点击「➕ 添加」时写回配置，返回是否新增成功。
 * :param options.addedText: 添加成功时的 Toast 文案（接收 ID）。
 * :param options.dupHint: 记录已存在时的 Toast 文案。
 * :return: { element, paint(items) }；paint 每次整体重绘，不会累加。
 */
function createRecentList(options) {
  const opts = options || {};
  const element = h("div", { class: "checkbox-list recent-list" });
  const idLabel = opts.idLabel || "ID";

  const paint = (items) => {
    clear(element);
    const list = toArray(items).filter((item) => item && String(item.id || "").trim());
    if (!list.length) {
      element.appendChild(h("div", { class: "inline-note", text: opts.emptyText || "暂无记录" }));
      return 0;
    }
    list.forEach((item) => {
      const id = String(item.id).trim();
      const title = typeof opts.titleOf === "function" ? opts.titleOf(item) : id;
      const meta = recentMeta(item);
      element.appendChild(
        h("div", { class: "recent-item" }, [
          h("span", { class: "ci-main" }, [
            h("span", { class: "ci-title", text: title }),
            h("div", { class: "hint mono", text: idLabel + "：" + id }),
            meta ? h("div", { class: "hint", text: meta }) : null,
          ]),
          h("button", {
            class: "btn tiny",
            type: "button",
            text: "➕ 添加",
            onclick: () => {
              const added = typeof opts.commit === "function" ? opts.commit(id) : false;
              if (added) {
                markDirty();
                toast(
                  typeof opts.addedText === "function"
                    ? opts.addedText(id)
                    : "已添加：" + id + "（保存后生效）",
                  "ok",
                );
              } else {
                toast(opts.dupHint || "该 ID 已在列表中", "warn");
              }
            },
          }),
        ]),
      );
    });
    return list.length;
  };

  return { element, paint };
}

/** 读取插件记录到的最近交互用户 / 群（运行期内存快照，最多各 30 条，最新在前）。 */
async function fetchRecent() {
  const data = await apiGet("recent");
  const result = {
    users: toArray(data && data.users),
    groups: toArray(data && data.groups),
  };
  state.recent = result;
  return result;
}

/* ------------------------------------------------------------ 公共小部件 */

/**
 * 创建一个「状态摘要卡片」，用于概览区的关键指标展示。
 */
function statCard(icon, label, value, sub, tone) {
  const shown = value === undefined || value === null || value === "" ? "—" : String(value);
  const subText = sub === undefined || sub === null || sub === "" ? "" : String(sub);
  return h("div", { class: "stat-card" + (tone ? " " + tone : "") }, [
    h("div", { class: "stat-label" }, [h("span", { text: icon }), h("span", { text: label })]),
    h("div", { class: "stat-value", title: shown, text: shown }),
    subText ? h("div", { class: "stat-sub", title: subText, text: subText }) : null,
  ]);
}

/** 带标签的输入框，返回可直接插入 DOM 的 field 节点。 */
function textField(labelText, inputNode, hintText, extraNodes) {
  return h("div", { class: "field" }, [
    h("label", { class: "field-label", text: labelText }),
    inputNode,
    hint(hintText),
    extraNodes || null,
  ]);
}

/** 读取当前供应商对象（按名称匹配）。 */
function supplierByName(name) {
  const target = String(name || "");
  if (!target) return null;
  return state.suppliers.find((item) => item.name === target) || null;
}

function activeSupplierName() {
  const cfg = state.config || {};
  return String(cfg.active_supplier || cfg.active_provider || "");
}

function modelCacheKey(supplier, protocol) {
  return JSON.stringify([
    String(supplier || ""),
    String(protocol || "openai"),
  ]);
}

function protocolShortLabelOf(key) {
  return PROTOCOL_SHORT_LABELS[key] || state.protocolLabels[key] || key || "未知协议";
}

function protocolLabelLongOf(key) {
  const label = state.protocolLabels[key];
  if (label && label !== PROTOCOL_SHORT_LABELS[key]) return label;
  return PROTOCOL_FALLBACK_LABELS[key] || label || key;
}

function protocolFormOf(key) {
  return PROTOCOL_FORMS[key] || "";
}

/** 当前协议下的模型配置（内存态，可能尚未保存）。 */
function currentProtocolModels(protocol) {
  const key = protocol || state.activeProtocol;
  const entry = state.protocolModels[key];
  if (entry) return entry;
  return { model: "", edit_model: "" };
}

function completionModel(protocol) {
  const models = currentProtocolModels(protocol);
  if (state.modelEditors && state.modelEditors.protocol === protocol && state.modelEditors.modelInput) {
    return String(state.modelEditors.modelInput.value || "").trim();
  }
  return String(models.model || "").trim();
}

function completionChecks() {
  const cfg = state.config || {};
  const supplierName = activeSupplierName();
  const supplier = supplierByName(supplierName);
  const protocol = String(state.activeProtocol || "openai");
  const localProtocol = SELFHOST_PROTOCOLS.indexOf(protocol) !== -1;
  const baseUrl = String(supplier && supplier.base_url || "").trim();
  const model = completionModel(protocol);
  const cached = (state.modelCache && state.modelCache[modelCacheKey(supplierName, protocol)]) || [];
  const sizes = trimList(cfg.image_sizes);
  const activeSize = String(cfg.active_size || "").trim();
  const drawCommands = trimList(cfg.draw_commands);
  const editCommands = trimList(cfg.edit_commands);
  const menuCommands = trimList(cfg.menu_commands);
  const sections = toArray(cfg.menu_sections).filter((item) => item && typeof item === "object");
  const menuItems = sections.reduce((total, item) => total + trimList(item.items).length, 0);
  const translateEnabled = cfg.translate_enabled === true;
  const translateSupplierName = String(cfg.translate_supplier || supplierName).trim();
  const translateSupplier = supplierByName(translateSupplierName);
  const translateModel = String(cfg.translate_model || "").trim();
  const visionEnabled = cfg.img2prompt_enabled === true;
  const visionModel = String(cfg.img2prompt_model || "").trim();

  return [
    { key: "supplier", ok: Boolean(supplierName && supplier), label: supplierName && supplier ? "已选择当前供应商" : "尚未选择有效供应商", detail: supplierName && supplier ? supplierName : "请在供应商列表中选择一个当前供应商" },
    { key: "endpoint", ok: Boolean(baseUrl && looksLikeUrl(baseUrl, false)), label: baseUrl ? "接口地址已填写" : "尚未填写接口地址", detail: baseUrl || "请填写供应商 Base URL" },
    { key: "credential", ok: Boolean(supplier && (supplier.api_key_set || localProtocol)), label: supplier && (supplier.api_key_set || localProtocol) ? "访问凭据已满足协议要求" : "尚未保存 API Key", detail: localProtocol ? "本地协议无需 API Key" : (supplier && supplier.api_key_set ? "密钥已保存" : "远程协议需要 API Key") },
    { key: "model", ok: Boolean(model || localProtocol), label: model || localProtocol ? "生成模型已配置" : "尚未配置生成模型", detail: model || (localProtocol ? "本地协议由服务端工作流或检查点决定" : "请填写模型 ID 或从模型列表选择") },
    { key: "model-catalog", ok: Boolean(cached.length || model), label: cached.length ? "模型目录已获取" : (model ? "已填写模型 ID，目录待验证" : "尚未获取模型目录"), detail: cached.length ? "当前协议已缓存 " + cached.length + " 个模型" : (model ? "供应商不提供目录时可继续使用手动模型 ID" : "保存供应商后获取模型列表，或手动填写模型 ID") },
    { key: "sizes", ok: Boolean(sizes.length && sizes.every(looksLikeSize) && (!activeSize || sizes.indexOf(activeSize) !== -1)), label: sizes.length ? "图片尺寸已配置" : "尚未配置图片尺寸", detail: sizes.length ? "可用尺寸 " + sizes.length + " 个" : "请至少保留一个有效尺寸" },
    { key: "commands", ok: Boolean(drawCommands.length && editCommands.length && menuCommands.length), label: drawCommands.length && editCommands.length && menuCommands.length ? "绘画、编辑、菜单指令完整" : "指令设置仍有缺项", detail: "绘画 " + drawCommands.length + " · 编辑 " + editCommands.length + " · 菜单 " + menuCommands.length },
    { key: "menu", ok: Boolean(sections.length && menuItems), label: sections.length && menuItems ? "菜单分组已配置" : "菜单分组暂无有效内容", detail: sections.length && menuItems ? sections.length + " 组 · " + menuItems + " 项" : "请添加至少一个分组和一条菜单指令" },
    { key: "translation", ok: !translateEnabled || Boolean(translateSupplier && translateModel), label: !translateEnabled ? "提示词翻译未启用" : (translateSupplier && translateModel ? "翻译模型已配置" : "翻译功能缺少配置"), detail: !translateEnabled ? "按需开启" : (translateSupplier && translateModel ? translateSupplierName + " · " + translateModel : "开启翻译后需要有效供应商和文本模型") },
    { key: "vision", ok: !visionEnabled || Boolean(visionModel), label: !visionEnabled ? "图片转提示词未启用" : (visionModel ? "视觉模型已配置" : "图片转提示词缺少视觉模型"), detail: !visionEnabled ? "按需开启" : (visionModel ? visionModel : "开启图片转提示词后需要填写视觉模型") },
  ];
}

function updateCompletion() {
  const percentNode = $("completion-percent");
  const summaryNode = $("completion-summary");
  const trackNode = $("completion-track");
  const fillNode = $("completion-track-fill");
  const liveNode = $("completion-live");
  if (!percentNode || !summaryNode || !trackNode || !fillNode) return;
  if (!state.config) {
    state.completion = null;
    percentNode.textContent = "状态未知";
    summaryNode.textContent = "无法读取配置，暂不能计算完成度";
    fillNode.style.width = "0%";
    trackNode.setAttribute("aria-valuenow", "0");
    trackNode.setAttribute("aria-valuetext", "配置状态未知");
    if (liveNode) liveNode.textContent = "配置状态未知，无法计算完成度";
    return;
  }
  const checks = completionChecks();
  const completed = checks.filter((item) => item.ok).length;
  const total = checks.length;
  const percent = total ? Math.round(completed / total * 100) : 0;
  const pending = checks.filter((item) => !item.ok).map((item) => item.label.replace(/^尚未|缺少|暂无/, "").trim());
  const summary = pending.length ? "待完善：" + pending.slice(0, 3).join("、") + (pending.length > 3 ? "等 " + pending.length + " 项" : "") : "全部模块已就绪";
  state.completion = { completed, total, percent, checks };
  percentNode.textContent = "完成 " + completed + "/" + total + " · " + percent + "%";
  summaryNode.textContent = summary;
  fillNode.style.width = percent + "%";
  trackNode.setAttribute("aria-valuenow", String(percent));
  trackNode.setAttribute("aria-valuetext", "已完成 " + completed + " 个模块，共 " + total + " 个，完成度 " + percent + "%");
  if (liveNode) liveNode.textContent = "配置完成度已更新为 " + percent + "%，已完成 " + completed + " 个模块，共 " + total + " 个";
}

/** 更新顶部摘要栏。 */
/** 写入顶部摘要胶囊：文本 + 语义色 + 悬停说明。 */
function paintPill(node, text, kind, title) {
  if (!node) return;
  node.textContent = String(text === undefined || text === null ? "" : text);
  node.className = "sum-pill " + (kind || "plain");
  if (title) node.setAttribute("title", String(title));
  else node.removeAttribute("title");
}

function paintSummary() {
  const cfg = state.config || {};
  const status = state.status || {};
  const supplier = activeSupplierName();
  const protocol = String(state.activeProtocol || "openai");
  const models = currentProtocolModels(protocol);
  const model = models.model || (status.model || "") || "—";
  const size = String(cfg.active_size || (status.active_size || "")) || "—";

  /* 供应商胶囊：重点提示「有没有填密钥」与「是不是当前使用中」。 */
  const supplierEntry = supplierByName(supplier);
  const keyReady = !!(supplierEntry && supplierEntry.api_key_set);
  paintPill(
    $("sum-supplier"),
    "供应商：" + (supplier || "未选择"),
    supplier ? (keyReady ? "ok" : "warn") : "err",
    supplier
      ? (keyReady
        ? "当前供应商已配置密钥"
        : "当前供应商还没填密钥：在「供应商与协议」里补填 API Key")
      : "还没有选择供应商：请在「供应商与协议」中新增一个中转站",
  );

  paintPill($("sum-protocol"), "协议：" + protocolShortLabelOf(protocol), "plain",
    "同一供应商可供多个协议使用，具体协议支持由服务商决定");

  /* 模型胶囊：提示是否已经联网获取过模型列表。 */
  const cached = (state.modelCache && state.modelCache[modelCacheKey(supplier, protocol)]) || [];
  const modelMeta = modelMetaFor(supplier, protocol);
  const diagnostic = state.supplierDiagnostics[modelCacheKey(supplier, protocol)] || {};
  paintPill(
    $("sum-model"),
    "模型：" + model,
    cached.length ? "ok" : "warn",
    cached.length
      ? "已从该供应商获取过 " + cached.length + " 个模型"
      : "尚未联网获取过模型列表：可在「供应商与协议」里点「📋 获取模型列表」",
  );

  paintPill($("sum-size"), "尺寸：" + size, "plain", "在「模型与尺寸」里可以增删尺寸");
  paintPill($("hero-supplier"), supplier || "供应商未选择", supplier ? (diagnostic.ok === false ? "err" : "ok") : "warn",
    diagnostic.message || (supplierEntry && supplierEntry.base_url ? supplierEntry.base_url : "请先配置供应商"));
  paintPill($("hero-model"), model === "—" ? "模型未设置" : model, model === "—" ? "warn" : "ok",
    cached.length ? "模型目录 " + (modelMeta && modelMeta.total ? modelMeta.total : cached.length) + " 个，最近更新 " + formatModelFetchedAt(modelMeta && modelMeta.fetchedAt) : "请获取模型列表");
  refreshSmartModelSources();
  updateCompletion();
}

/* ---------------------------------------------------------------- 概览区 */

function renderOverview() {
  const host = $("overview-body");
  if (!host) return;
  clear(host);

  const cfg = state.config || {};
  const status = state.status || {};
  const stats = state.stats || {};
  const supplierName = activeSupplierName();
  const supplier = supplierByName(supplierName);
  const protocol = String(state.activeProtocol || "openai");
  const models = currentProtocolModels(protocol);
  const size = String(cfg.active_size || "");
  const sizes = trimList(cfg.image_sizes);

  const checks = completionChecks();

  const list = h("div", { class: "checklist" }, checks.map((item) => {
    const kind = item.ok ? "ok" : "warn";
    return h("div", { class: "check-item " + kind }, [
      h("span", { class: "check-icon", "aria-hidden": "true", text: item.ok ? "✅" : (item.warn ? "⚠️" : "⛔") }),
      h("span", { class: "check-text" }, [
        h("span", { class: "check-label", text: item.label }),
        h("span", { class: "check-detail", text: item.detail }),
      ]),
    ]);
  }));
  const problemCount = checks.filter((item) => !item.ok).length;
  const healthTone = problemCount === 0 ? "tone-ok" : (problemCount <= 2 ? "tone-warn" : "tone-err");
  const healthCard = h("div", { class: "card " + healthTone }, [
    h("div", { class: "card-head" }, [
      iconTile(problemCount === 0 ? "✅" : "🩺", "sm"),
      h("div", {}, [
        h("h3", { text: "🩺 配置健康检查" }),
        h("div", { class: "hint", text: problemCount === 0
          ? "全部检查项都已通过，可以直接在群里发送绘画指令了。"
          : "还有 " + problemCount + " 项需要处理，按下面的提示补齐即可。" }),
      ]),
      h("span", { class: "spacer" }),
      problemCount === 0 ? pill("全部就绪", "ok") : pill("待完善 " + problemCount + " 项", "warn"),
    ]),
    h("div", { class: "card-body" }, [list]),
  ]);
  host.appendChild(healthCard);

  const totals = stats.totals || stats;
  const sessionTotals = stats.session_totals || stats.sessions || stats.status_counts || {};
  const total = sessionTotals.total === undefined ? (totals.total === undefined ? status.total : totals.total) : sessionTotals.total;
  const success = sessionTotals.success === undefined ? (totals.success === undefined ? status.success : totals.success) : sessionTotals.success;
  const failed = sessionTotals.failed === undefined ? (totals.failed === undefined ? status.failed : totals.failed) : sessionTotals.failed;
  const partial = sessionTotals.partial === undefined ? (totals.partial || 0) : sessionTotals.partial;
  const cancelled = sessionTotals.cancelled === undefined ? (totals.cancelled || 0) : sessionTotals.cancelled;
  const lastTime = totals.last_time || stats.updated_at || "";

  const card = h("div", { class: "card" }, [
    h("div", { class: "card-head" }, [
      iconTile("📊", "sm"),
      h("div", {}, [
        h("h3", { text: "📊 当前运行状态" }),
        h("div", { class: "hint", text: "以下信息来自插件运行期状态与配置（未保存的修改不会显示在这里）。" }),
      ]),
      h("div", { class: "row tight" }, [
        h("button", {
          class: "btn tiny",
          type: "button",
          text: "🔄 刷新状态",
          onclick: async (event) => {
            const button = event.currentTarget;
            setBusy(button, true, "刷新中…");
            try {
              const results = await Promise.allSettled([apiGet("status"), apiGet("stats")]);
              const failures = [];
              const statusResult = results[0];
              if (statusResult.status !== "fulfilled") {
                failures.push("状态读取失败：" + errText(statusResult.reason));
              } else if (statusResult.value && statusResult.value.ok === false) {
                failures.push("状态读取失败：" + String(statusResult.value.message || "接口返回失败"));
              } else {
                state.status = statusResult.value || {};
              }
              const statsResult = results[1];
              if (statsResult.status !== "fulfilled") {
                failures.push("统计读取失败：" + errText(statsResult.reason));
              } else if (statsResult.value && statsResult.value.ok === false) {
                failures.push("统计读取失败：" + String(statsResult.value.message || "接口返回失败"));
              } else {
                state.stats = statsResult.value || {};
              }
              renderOverview();
              paintSummary();
              if (failures.length) {
                const message = failures.join("；");
                showError(message);
                toast(message, "err");
              } else {
                clearError();
                toast("状态与统计已刷新", "ok");
              }
            } catch (error) {
              showError("刷新失败：" + errText(error));
              toast("刷新失败：" + errText(error), "err");
            } finally {
              setBusy(button, false);
            }
          },
        }),
        h("button", {
          class: "btn tiny",
          type: "button",
          text: "🔄 重载配置",
          title: "提示：本按钮只刷新页面数据，真正重载插件请在 AstrBot 插件管理里操作",
          onclick: async () => {
            const ok = await confirmDialog({
              title: "重载配置",
              message: "本页面无法直接重载插件进程，请到 AstrBot「插件管理」中点击重载，然后回到本页刷新。",
              danger: "确认后本页会重新读取服务端配置，未保存的修改将丢失。",
              confirmText: "重新读取",
              confirmKind: "primary",
            });
            if (!ok) return;
            const loaded = await loadAll();
            if (loaded) {
              toast("已重新读取服务端配置，未保存修改已清除；完整重载请在 AstrBot 插件管理中操作", "ok");
            }
          },
        }),
      ]),
    ]),
    h("div", { class: "card-body" }, [
      h("div", { class: "stat-grid" }, [
        statCard("🔌", "当前供应商", supplierName || "未选择", supplier
          ? (supplier.api_key_set ? "密钥已保存：" + (supplier.api_key_masked || "••••") : "尚未保存密钥")
          : "请在「供应商与协议」中添加",
          supplierName ? (supplier && supplier.api_key_set ? "tone-ok" : "tone-warn") : "tone-err"),
        statCard("🧩", "当前协议", protocolShortLabelOf(protocol), protocolFormOf(protocol)),
        statCard("🧠", "生成模型", models.model || "—", models.edit_model ? "编辑模型：" + models.edit_model : "编辑模型同生成模型"),
        statCard("📐", "当前尺寸", size || "—", "共 " + sizes.length + " 个可选尺寸"),
        statCard("⏳", "接口超时", Math.round(num(cfg.timeout, 600)) + " 秒", "最大并发出图：" + num(cfg.max_concurrent, 2) + " 个"),
        statCard("🛡", "主人数量", String(trimList(cfg.masters).length) + " 人",
          "AstrBot 管理员" + (cfg.masters_use_astrbot_admin === false ? "不" : "") + "自动视为主人",
          trimList(cfg.masters).length ? "tone-ok" : "tone-warn"),
      ]),
      h("div", { class: "sub-block" }, [
        h("div", { class: "sub-title", text: "🧭 快捷指引" }),
        h("div", { class: "inline-note", text:
          "配置流程：先在「供应商与协议」顶部选择协议，再填写 Base URL 与 API Key（填完先点右上角「💾 保存配置」），最后在「当前协议的模型设置」里点「📋 获取模型列表」选择模型。协议之间共用同一份域名与密钥，切换协议不需要重新填写。" }),
        h("div", { class: "row", style: "margin-top:10px" }, [
          h("button", {
            class: "btn tiny",
            type: "button",
            text: "↗ 前往供应商与协议",
            onclick: () => scrollToSection("#section-suppliers"),
          }),
          h("button", {
            class: "btn tiny",
            type: "button",
            text: "↗ 前往权限与范围",
            onclick: () => scrollToSection("#section-access"),
          }),
          h("button", {
            class: "btn tiny",
            type: "button",
            text: "↗ 前往使用说明",
            onclick: () => scrollToSection("#section-help"),
          }),
        ]),
      ]),
      h("div", { class: "sub-block" }, [
        h("div", { class: "row tight" }, [
          h("div", { class: "sub-title", text: "📈 运行统计", style: "margin:0" }),
          h("span", { class: "spacer" }),
          h("button", {
            class: "btn tiny",
            type: "button",
            text: "📊 刷新统计",
            onclick: async (event) => {
              const button = event.currentTarget;
              setBusy(button, true, "读取中…");
              try {
                state.stats = requireApiSuccess(await apiGet("stats"), "统计接口返回失败");
                renderOverview();
                toast("运行统计已更新", "ok");
              } catch (error) {
                showError("读取统计失败：" + errText(error));
                toast("读取统计失败：" + errText(error), "err");
              } finally {
                setBusy(button, false);
              }
            },
          }),
        ]),
        total === undefined && success === undefined
          ? h("div", { class: "inline-note", style: "margin-top:8px", text:
              "暂未读取到运行统计。出图几次后点上面的「📊 刷新统计」，或在群里让主人发送「运行统计」指令。" })
          : h("div", { class: "stat-grid", style: "margin-top:8px" }, [
              statCard("🧾", "会话总数", total === undefined ? "—" : total, "一次绘画或编辑请求计为一个会话"),
              statCard("✅", "会话成功", success === undefined ? "—" : success, "完整会话成功"),
              statCard("❌", "会话失败", failed === undefined ? "—" : failed, "完整会话失败"),
              statCard("◐", "部分成功", partial, "批量会话中部分任务成功"),
              statCard("⏹", "已取消", cancelled, "主动取消或任务被取消"),
              statCard("🖼", "累计出图", totals.images === undefined ? "—" : totals.images, "成功生成的图片数量"),
              statCard("🕒", "最近一次", lastTime || "—", "本地时间"),
            ]),
      ]),
      h("div", { class: "sub-block" }, [
        h("div", { class: "row tight" }, [
          h("div", { class: "sub-title", text: "🧰 便捷操作", style: "margin:0" }),
          h("span", { class: "spacer" }),
          h("button", {
            class: "btn tiny danger",
            type: "button",
            text: "↩️ 全部恢复默认",
            onclick: async () => {
              const ok = await confirmDialog({
                title: "全部恢复默认",
                message: "将把指令、文案、尺寸、权限范围、触发方式、冷却、重试与代理等全部重置为插件内置默认值。",
                danger: "供应商列表与 API Key、当前协议模型也会一起重置，且需要点击「保存配置」后才会真正写入。",
                confirmText: "确认恢复默认",
              });
              if (!ok) return;
              applyAllDefaults();
            },
          }),
        ]),
        h("div", { class: "hint", style: "margin-top:6px", text:
          "仅重置本页面内存中的表单内容，点右上角「💾 保存配置」后才会写入插件配置。" }),
      ]),
    ]),
  ]);

  host.appendChild(card);
}

function scrollToSection(selector, options) {
  const opts = options || {};
  const sections = Array.from(document.querySelectorAll(".content > .section"));
  const section = sections.find((item) => "#" + item.id === selector || item.id === selector);
  if (!section) return;
  state.activeSectionId = section.id;
  updatePageVisibility();
  if (!opts.fromHistory && window.location.hash !== selector) {
    try {
      window.history.pushState(null, "", "#" + section.id);
    } catch (error) {
      window.location.hash = section.id;
    }
  }
  if (opts.focus !== false && typeof section.focus === "function") {
    section.focus({ preventScroll: true });
  }
}

/* ---------------------------------------------------- 供应商与协议 */

/** 切换当前协议：先把模型输入框的值写回内存，再整体重绘。 */
function setActiveProtocol(protocol) {
  if (PROTOCOL_KEYS.indexOf(protocol) === -1) return;
  syncModelInputsToState();
  state.activeProtocol = protocol;
  if (state.config) state.config.active_protocol = protocol;
  markDirty();
  renderSuppliers();
  renderSizePreview();
  paintSummary();
  toast("已切换到协议：" + protocolShortLabelOf(protocol) + "（域名与密钥共用，模型按协议分别保存）", "info");
}

/** 把「当前协议的模型设置」输入框内容写回内存状态，避免切换协议时丢失。 */
function syncModelInputsToState() {
  const editors = state.modelEditors;
  if (!editors) return;
  const target = currentProtocolModels(editors.protocol);
  if (editors.modelInput) target.model = String(editors.modelInput.value || "").trim();
  if (editors.editModelInput) target.edit_model = String(editors.editModelInput.value || "").trim();
}

/** 协议切换器：三个卡片式分段按钮，显示请求形态与各自已保存的模型。 */
function createProtocolSwitcher() {
  const tabs = h("div", { class: "protocol-tabs", role: "radiogroup", "aria-label": "接口协议切换器" });

  state.protocolOrder.forEach((key) => {
    const active = key === state.activeProtocol;
    const models = currentProtocolModels(key);
    tabs.appendChild(
      h("button", {
        class: "protocol-tab" + (active ? " active" : ""),
        type: "button",
        role: "radio",
        "aria-checked": active ? "true" : "false",
        title: "切换到 " + protocolShortLabelOf(key),
        onclick: () => setActiveProtocol(key),
      }, [
        h("span", { class: "protocol-radio", "aria-hidden": "true" }),
        h("span", { class: "protocol-main" }, [
          h("span", { class: "protocol-name" }, [
            h("span", { text: protocolShortLabelOf(key) }),
            active ? badge("当前协议", "ok") : null,
          ]),
          h("div", { class: "protocol-form", text: protocolFormOf(key) }),
          h("div", { class: "protocol-model" }, [
            h("span", { class: "hint", text: "模型：" }),
            models.model ? h("code", { text: models.model }) : h("span", { class: "hint", text: "未设置" }),
          ]),
        ]),
      ]),
    );
  });

  return tabs;
}

/** 刷新「供应商列表」卡片头部的统计徽标（改名 / 切换当前供应商后调用）。 */
function paintSupplierListHead() {
  const countNode = $("supplier-count-badge");
  if (countNode) countNode.textContent = state.suppliers.length + " 个供应商";
  const activeNode = $("supplier-active-badge");
  if (activeNode) activeNode.textContent = "当前：" + (activeSupplierName() || "未选择");
}

function setSupplierDiagnostic(supplier, protocol, patch) {
  const key = modelCacheKey(supplier, protocol);
  state.supplierDiagnostics[key] = {
    ...(state.supplierDiagnostics[key] || {}),
    ...(patch || {}),
    checkedAt: new Date().toISOString(),
  };
  updateCompletion();
}

function paintModelMeta(resultBox, supplier, protocol) {
  const key = modelCacheKey(supplier, protocol);
  const models = state.modelCache[key] || [];
  const meta = state.modelMeta[key];
  if (!models.length || !meta) return;
  setResult(resultBox,
    (meta.cached ? "已使用缓存模型 " : "已获取 ") + (meta.total || models.length) + " 个模型 · 更新时间：" + formatModelFetchedAt(meta.fetchedAt),
    meta.cached ? "info" : "ok");
}

/** 供应商卡片：只有 名称 / Base URL / API Key 三个输入框 + 操作按钮。 */
function createSupplierCard(supplier, index) {
  const resultBox = h("div", { class: "result" });
  const card = h("div", { class: "supplier-card" });

  const titleNode = h("span", { class: "supplier-title", text: supplier.name || "未命名供应商" });
  const statusNode = h("span", { class: "row tight", style: "margin:0" });
  const radioInput = h("input", { type: "radio", name: "active-supplier" });

  const isActiveNow = () => supplier.name !== "" && supplier.name === activeSupplierName();

  /** 单独刷新卡片头部状态（不重建输入框，避免正在输入的内容丢失）。 */
  const paintStatus = () => {
    const host = hostOf(supplier.base_url);
    const pendingKey = String(supplier.api_key || "").trim();
    card.className = "supplier-card" + (isActiveNow() ? " current" : "");
    if (host) card.setAttribute("title", supplier.name + " · " + host);
    else card.removeAttribute("title");
    titleNode.textContent = supplier.name || "未命名供应商";
    titleNode.setAttribute("title", supplier.name || "未命名供应商");
    radioInput.checked = isActiveNow();
    clear(statusNode);
    if (isActiveNow()) statusNode.appendChild(badge("当前使用", "ok"));
    if (pendingKey) statusNode.appendChild(badge("待保存密钥", "info"));
    else if (supplier.api_key_set) statusNode.appendChild(badge("密钥已保存", "ok"));
    else statusNode.appendChild(badge("未配置密钥", "warn"));
    if (!String(supplier.base_url || "").trim()) statusNode.appendChild(badge("缺少接口地址", "err"));
    const diagnostic = state.supplierDiagnostics[modelCacheKey(supplier.name, state.activeProtocol)];
    if (diagnostic && diagnostic.pending) statusNode.appendChild(badge("检查中", "info"));
    else if (diagnostic && diagnostic.ok === true) statusNode.appendChild(badge("连接正常", "ok"));
    else if (diagnostic && diagnostic.ok === false) statusNode.appendChild(badge("连接失败", "err"));
    const meta = modelMetaFor(supplier.name, state.activeProtocol);
    if (meta && state.modelCache[modelCacheKey(supplier.name, state.activeProtocol)] && state.modelCache[modelCacheKey(supplier.name, state.activeProtocol)].length) {
      statusNode.appendChild(badge((meta.cached ? "缓存 " : "模型 ") + (meta.total || state.modelCache[modelCacheKey(supplier.name, state.activeProtocol)].length), "info"));
    }
    paintHostChip(host);
  };

  /* 把刷新函数挂到供应商对象上，便于单选 / 改名时统一刷新所有卡片状态。 */
  supplier.__paintStatus = paintStatus;

  /* 域名胶囊：让用户一眼看出这个供应商指向哪个中转站。 */
  const hostChip = h("span", { class: "host-chip", hidden: true });
  const paintHostChip = (host) => {
    if (!host) {
      hostChip.hidden = true;
      hostChip.textContent = "";
      return;
    }
    hostChip.hidden = false;
    hostChip.textContent = host;
    hostChip.setAttribute("title", "接口地址：" + String(supplier.base_url || ""));
  };

  const head = h("div", { class: "supplier-head" }, [
    h("span", { class: "supplier-index", text: String(index + 1) }),
    h("span", { class: "supplier-head-main" }, [titleNode, hostChip]),
    statusNode,
    h("span", { class: "spacer" }),
    h("button", {
      class: "btn tiny danger",
      type: "button",
      text: "🗑 删除",
      onclick: async () => {
        const ok = await confirmDialog({
          title: "删除供应商",
          message: "即将从列表中移除供应商「" + (supplier.name || "未命名供应商") + "」。",
          danger: "删除后需要点击「💾 保存配置」才会写入；若它是当前使用的供应商，会自动改为列表中的第一个。",
          confirmText: "确认删除",
        });
        if (!ok) return;
        const target = state.suppliers.indexOf(supplier);
        if (target === -1) return;
        state.suppliers.splice(target, 1);
        const cfg = state.config || {};
        if (activeSupplierName() === supplier.name) {
          const next = state.suppliers[0];
          cfg.active_supplier = next ? next.name : "";
        }
        markDirty();
        renderSuppliers();
        renderOverview();
        paintSummary();
        toast("已删除供应商「" + (supplier.name || "未命名供应商") + "」（保存配置后生效）", "warn");
      },
    }),
  ]);

  const nameInput = h("input", {
    class: "input",
    type: "text",
    value: supplier.name || "",
    placeholder: "供应商名称（唯一，例如 我的中转站）",
    oninput: (event) => {
      const previous = supplier.name;
      const next = event.currentTarget.value;
      supplier.name = next;
      /* 改名时同步「当前使用」标记，避免出现「当前供应商不在列表中」的校验错误。 */
      const cfg = state.config || {};
      if (String(cfg.active_supplier || "") === previous || !String(cfg.active_supplier || "")) {
        cfg.active_supplier = next;
      }
      if (String(cfg.translate_supplier || "") === previous) {
        cfg.translate_supplier = next;
      }
      paintStatus();
      paintSupplierListHead();
      markDirty();
      paintSummary();
    },
    onblur: () => {
      /* 失焦时再整体刷新一次，保证「当前使用」单选与当前供应商名称同步。 */
      paintStatus();
      paintSupplierListHead();
      renderOverview();
      paintSummary();
    },
  });

  const urlInput = h("input", {
    class: "input",
    type: "text",
    inputmode: "url",
    value: supplier.base_url || "",
    placeholder: "https://api.example.com/v1",
    oninput: (event) => {
      supplier.base_url = event.currentTarget.value;
      paintStatus();
      markDirty();
    },
  });

  const keyInput = h("input", {
    class: "input",
    type: "password",
    autocomplete: "off",
    value: "",
    placeholder: supplier.api_key_set
      ? (supplier.api_key_masked || "已保存密钥") + "（留空表示不修改）"
      : "填写中转站密钥 / KEY（留空表示不修改）",
    oninput: (event) => {
      supplier.api_key = event.currentTarget.value;
      if (String(event.currentTarget.value || "") !== "") supplier.api_key_clear = false;
      paintStatus();
      markDirty();
    },
  });

  /* 显式清空密钥：因为「留空」的语义是「不修改」，所以要单独给一个入口。 */
  const clearKeyButton = h("button", {
    class: "btn tiny ghost",
    type: "button",
    text: "🧹 清空密钥",
    title: supplier.api_key_set
      ? "把已保存的密钥清空（保存配置后生效）"
      : "当前没有已保存的密钥",
    onclick: async () => {
      if (!supplier.api_key_set) {
        setResult(resultBox, "ℹ️ 该供应商当前没有已保存的密钥，无需清空。", "info");
        return;
      }
      const ok = await confirmDialog({
        title: "清空密钥",
        message: "即将清空供应商「" + (supplier.name || "未命名供应商") + "」已保存的 API Key。",
        danger: "保存后该供应商将无法通过鉴权，需要重新填写密钥。",
        confirmText: "确认清空",
      });
      if (!ok) return;
      supplier.api_key = "";
      supplier.api_key_clear = true;
      keyInput.value = "";
      paintStatus();
      markDirty();
      setResult(resultBox, "✅ 已标记清空密钥，点击「💾 保存配置」后生效。", "ok");
    },
  });

  const pickerHost = h("div", { class: "model-picker", hidden: true });

  const actionRow = h("div", { class: "row tight", style: "margin-top:12px" }, [
    h("button", {
      class: "btn tiny",
      type: "button",
      text: "🔗 测试连接",
      onclick: async (event) => {
         const button = event.currentTarget;
         const targetProtocol = String(state.activeProtocol || "openai");
         setSupplierDiagnostic(supplier.name, targetProtocol, { pending: true, message: "正在诊断连接" });
         paintStatus();
         setBusy(button, true, "测试中…");
        setResult(resultBox, "正在测试连接（读取的是已保存的供应商配置）…", "info");
         try {
           const targetName = String(supplier.name || "").trim();
           const data = await apiPost("suppliers/test", { supplier: targetName, protocol: targetProtocol });
           const ok = data && data.ok !== false;
           const message = (data && data.message) || (ok ? "连接正常" : "连接失败");
           setSupplierDiagnostic(targetName, targetProtocol, { ok, message, latency: data && data.latency });
           setResult(resultBox, (ok ? "✅ " : "❌ ") + message + "（协议：" + protocolShortLabelOf(targetProtocol) + "）", ok ? "ok" : "err");
           const parsed = readModelsResponse(data);
           const models = parsed.models;
           if (models.length) {
             rememberModels(targetName, targetProtocol, parsed, "连接诊断");
             renderModelPicker(targetName, models, pickerHost);
           }
           paintStatus();
           paintSummary();
         } catch (error) {
           setSupplierDiagnostic(supplier.name, state.activeProtocol, { ok: false, message: errText(error) });
           setResult(resultBox, "❌ 连接失败：" + errText(error), "err");
           paintStatus();
           paintSummary();
         } finally {
           setSupplierDiagnostic(supplier.name, state.activeProtocol, { pending: false });
           paintStatus();
           setBusy(button, false);
        }
      },
    }),
    h("button", {
      class: "btn tiny",
      type: "button",
      text: "📋 获取模型列表",
      onclick: async (event) => {
         const button = event.currentTarget;
         const targetProtocol = String(state.activeProtocol || "openai");
         setSupplierDiagnostic(supplier.name, targetProtocol, { pending: true, message: "正在获取模型列表" });
         paintStatus();
         setBusy(button, true, "获取中…");
        setResult(resultBox, "正在获取模型列表（读取的是已保存的供应商配置，请先保存）…", "info");
         try {
            const targetName = String(supplier.name || "").trim();
           const modelResult = await apiGet("models", { supplier: targetName, protocol: targetProtocol, refresh: 1 });
           if (!modelResult.ok) {
             setSupplierDiagnostic(targetName, targetProtocol, { ok: false, message: modelResult.message || "模型接口返回失败" });
             if (state.modelCache[modelCacheKey(targetName, targetProtocol)] && state.modelCache[modelCacheKey(targetName, targetProtocol)].length) {
               paintModelMeta(resultBox, targetName, targetProtocol);
               setResult(resultBox, "⚠️ 在线获取失败，已回退到本地缓存（" + (state.modelCache[modelCacheKey(targetName, targetProtocol)] || []).length + " 个）", "warn");
             } else setResult(resultBox, "⚠️ 获取模型失败：" + modelResult.message, "err");
           } else {
             rememberModels(targetName, targetProtocol, modelResult, "在线接口");
             setSupplierDiagnostic(targetName, targetProtocol, { ok: true, message: "模型接口可用" });
             setResult(resultBox, "✅ 获取到 " + modelResult.models.length + " 个模型 · 更新于 " + formatModelFetchedAt(modelMetaFor(targetName, targetProtocol).fetchedAt), "ok");
             renderModelPicker(targetName, modelResult.models, pickerHost);
           }
           paintStatus();
           paintSummary();
         } catch (error) {
           setSupplierDiagnostic(supplier.name, state.activeProtocol, { ok: false, message: errText(error) });
           setResult(resultBox, "❌ 获取失败：" + errText(error), "err");
           paintStatus();
           paintSummary();
         } finally {
           setSupplierDiagnostic(supplier.name, state.activeProtocol, { pending: false });
           paintStatus();
           setBusy(button, false);
        }
      },
    }),
  ]);

  const body = h("div", { class: "supplier-body" }, [
    h("div", { class: "grid" }, [
      textField("供应商名称", nameInput, "名称唯一，可随时改名；改名后请重新确认「当前使用」的供应商。"),
      textField("Base URL（接口地址）", urlInput, "中转站通常只填域名即可。自部署服务填写实际地址和端口。"),
      textField(
        "API Key（密钥）",
        h("div", { class: "field-row" }, [
          h("span", { class: "field-row-main" }, [keyInput]),
          h("span", { class: "field-row-extra" }, [clearKeyButton]),
        ]),
        "留空表示沿用该供应商已保存的密钥。需要置空时点右侧「清空密钥」。",
      ),
    ]),
    h("div", { class: "supplier-picker", style: "margin-top:12px" }, [
      h("label", { class: "supplier-radio" }, [
        (function () {
          radioInput.onchange = () => {
            const cfg = state.config || {};
            cfg.active_supplier = supplier.name;
            markDirty();
            state.suppliers.forEach((item) => {
              if (item.__paintStatus) item.__paintStatus();
            });
            paintSupplierListHead();
            renderOverview();
            paintSummary();
            toast("已将「" + (supplier.name || "未命名供应商") + "」设为当前供应商（记得保存配置）", "info");
          };
          radioInput.checked = isActiveNow();
          return radioInput;
        })(),
        h("span", { text: "设为此供应商（当前使用）" }),
      ]),
      h("span", { class: "spacer" }),
      h("span", { class: "hint", text: "当前供应商必须存在于列表中，保存前会校验。" }),
    ]),
    actionRow,
    pickerHost,
    resultBox,
  ]);

  card.appendChild(head);
  card.appendChild(body);

  /* 首次渲染就要把「当前使用 / 密钥 / 域名」状态画好，否则要等一次交互才出现。 */
  paintStatus();

  const cacheKey = modelCacheKey(supplier.name, state.activeProtocol);
  if (state.modelCache[cacheKey] && state.modelCache[cacheKey].length) {
    renderModelPicker(String(supplier.name || ""), state.modelCache[cacheKey], pickerHost);
  }

  return card;
}

/**
 * 模型标签面板：点击某个模型标签，写入当前聚焦的模型输入框（生成 / 编辑）。
 */
function renderModelPicker(supplierName, models, host) {
  if (!host) return;
  clear(host);
  host.hidden = false;

  const list = h("div", { class: "model-list" });
  const resultBox = h("div", { class: "result" });

  const paint = (needle) => {
    clear(list);
    const text = String(needle || "").trim().toLowerCase();
    const shown = models.filter((name) => !text || name.toLowerCase().includes(text));
    if (!shown.length) {
      list.appendChild(h("span", { class: "hint", text: "没有匹配的模型" }));
      return;
    }
    shown.forEach((name) => {
      const position = models.indexOf(name);
      list.appendChild(
        h("button", {
          class: "chip model-item",
          type: "button",
          title: "点击写入当前聚焦的模型输入框（聊天里也可发送「切换模型 " + (position >= 0 ? position + 1 : "序号") + "」）",
          onclick: () => {
            const written = applyModelToFocusedInput(name);
            if (written) {
              setResult(resultBox, "✅ 已写入「" + written + "」：" + name + "（保存配置后生效）", "ok");
              toast("已写入模型名：" + name + "（保存配置后生效）", "ok");
            } else {
              setResult(resultBox, "⚠️ 请先点击「生成模型」或「编辑模型」输入框，再点模型标签", "err");
            }
          },
        }, [
          position >= 0 ? h("span", { class: "model-index", text: String(position + 1) }) : null,
          h("span", { text: name }),
        ]),
      );
    });
  };

  const keyword = h("input", {
    class: "input",
    type: "text",
    placeholder: "筛选模型名…",
    style: "max-width:220px",
    oninput: (event) => paint(event.currentTarget.value),
  });

  const meta = modelMetaFor(supplierName, state.activeProtocol);
  host.appendChild(h("div", { class: "row tight" }, [
    h("span", { class: "hint", text: "「" + (supplierName || "当前供应商") + "」返回 " + (meta && meta.total ? meta.total : models.length) + " 个模型（点标签写入输入框；群内也可用「切换模型 序号」）：" }),
    keyword,
  ]));
  host.appendChild(h("div", { class: "hint", text: (meta && meta.cached ? "来源：服务端/本地缓存" : "来源：在线接口") + " · 最近更新：" + formatModelFetchedAt(meta && meta.fetchedAt) }));
  host.appendChild(list);
  host.appendChild(resultBox);
  paint("");
}

/** 把模型名写入用户最后聚焦的那个模型输入框（生成模型 / 编辑模型）。 */
function applyModelToFocusedInput(value) {
  const editors = state.modelEditors;
  if (!editors) return "";
  if (editors.lastFocus === "edit_model" && editors.editModelInput) {
    editors.editModelInput.value = value;
    currentProtocolModels(editors.protocol).edit_model = value;
    markDirty();
    return "编辑模型";
  }
  if (editors.modelInput) {
    editors.modelInput.value = value;
    currentProtocolModels(editors.protocol).model = value;
    markDirty();
    paintSummary();
    return "生成模型";
  }
  return "";
}

/** 「当前协议的模型设置」：生成模型 + 编辑模型 + 获取模型列表。 */
function createModelSection() {
  const protocol = state.activeProtocol;
  const models = currentProtocolModels(protocol);
  const supplierName = activeSupplierName();
  const resultBox = h("div", { class: "result" });
  const pickerHost = h("div", { class: "model-picker", hidden: true });

  const modelInput = h("input", {
    class: "input",
    id: "protocol-model",
    type: "text",
    value: models.model || "",
    placeholder: "例：" + ((DEFAULT_PROTOCOL_MODELS[protocol] || {}).model || "模型名"),
    oninput: (event) => {
      currentProtocolModels(protocol).model = event.currentTarget.value;
      state.modelEditors.lastFocus = "model";
      markDirty();
      paintSummary();
    },
    onfocus: () => {
      state.modelEditors.lastFocus = "model";
    },
  });

  const editModelInput = h("input", {
    class: "input",
    id: "protocol-edit-model",
    type: "text",
    value: models.edit_model || "",
    placeholder: "留空则与生成模型相同",
    oninput: (event) => {
      currentProtocolModels(protocol).edit_model = event.currentTarget.value;
      state.modelEditors.lastFocus = "edit_model";
      markDirty();
    },
    onfocus: () => {
      state.modelEditors.lastFocus = "edit_model";
    },
  });

  state.modelEditors = {
    protocol,
    modelInput,
    editModelInput,
    lastFocus: (state.modelEditors && state.modelEditors.lastFocus) || "model",
  };

  const buttons = h("div", { class: "row tight", style: "margin-top:10px" }, [
    h("button", {
      class: "btn tiny",
      type: "button",
      text: "📋 获取模型列表",
      onclick: async (event) => {
        const button = event.currentTarget;
        setBusy(button, true, "获取中…");
        setResult(resultBox, "正在获取模型列表（读取的是已保存的供应商配置，请先保存）…", "info");
        try {
          if (!supplierName) throw new Error("尚未选择供应商");
          const modelResult = await apiGet("models", { supplier: supplierName, protocol: protocol, refresh: 1 });
          if (!modelResult.ok) {
            const cached = state.modelCache[modelCacheKey(supplierName, protocol)] || [];
            setResult(resultBox, cached.length ? "⚠️ 在线获取失败，当前显示缓存 " + cached.length + " 个模型" : "⚠️ 获取模型失败：" + modelResult.message, cached.length ? "warn" : "err");
          } else {
            rememberModels(supplierName, protocol, modelResult, "在线接口");
            setSupplierDiagnostic(supplierName, protocol, { ok: true, message: "模型接口可用" });
            setResult(resultBox, "✅ 获取到 " + modelResult.models.length + " 个模型 · 更新于 " + formatModelFetchedAt(modelMetaFor(supplierName, protocol).fetchedAt), "ok");
            renderModelPicker(supplierName, modelResult.models, pickerHost);
          }
          paintSummary();
          renderSuppliers();
        } catch (error) {
          setSupplierDiagnostic(supplierName, protocol, { ok: false, message: errText(error) });
          setResult(resultBox, "❌ 获取失败：" + errText(error), "err");
          paintSummary();
        } finally {
          setBusy(button, false);
        }
      },
    }),
    h("button", {
      class: "btn tiny ghost",
      type: "button",
      text: "↩ 恢复该协议默认模型",
      onclick: () => {
        const fallback = DEFAULT_PROTOCOL_MODELS[protocol] || { model: "", edit_model: "" };
        const target = currentProtocolModels(protocol);
        target.model = fallback.model || "";
        target.edit_model = fallback.edit_model || "";
        markDirty();
        renderSuppliers();
        paintSummary();
        toast("已恢复 " + protocolShortLabelOf(protocol) + " 的默认模型（保存后生效）", "info");
      },
    }),
  ]);

  return h("div", {}, [
    h("div", { class: "row tight", style: "margin-bottom:10px" }, [
      h("div", { class: "sub-title", style: "margin:0", text: "🧠 当前协议的模型设置" }),
      badge(protocolShortLabelOf(protocol), "plain"),
      h("span", { class: "spacer" }),
      h("span", { class: "hint", text: "模型按协议分别保存，切换协议不会丢失。" }),
    ]),
    h("div", { class: "grid" }, [
      textField("生成模型", modelInput, "文生图使用的模型名；可点下方「📋 获取模型列表」后点选。"),
      textField("编辑模型（留空同生成模型）", editModelInput, "图片编辑使用的模型名；留空表示与生成模型相同。"),
    ]),
    buttons,
    h("div", { class: "inline-note", style: "margin-top:10px", text:
      "提示：获取模型列表读取的是「已保存」的供应商配置，请先点右上角「💾 保存配置」；" +
      "点击模型标签会写入你最后点击的输入框（生成模型 / 编辑模型）。" }),
    pickerHost,
    resultBox,
  ]);
}

function renderSuppliers() {
  const host = $("suppliers-body");
  if (!host) return;
  clear(host);

  /* ---- 上半部分：协议切换器 ---- */
  const protocolCard = h("div", { class: "card" }, [
    h("div", { class: "card-head" }, [
      h("div", {}, [
        h("h3", { text: "🧩 协议切换器" }),
        h("div", { class: "hint", text: "八种协议分别保存模型。选择服务商实际支持的协议；使用本地服务时请切换到对应供应商地址。" }),
      ]),
      badge("当前：" + protocolShortLabelOf(state.activeProtocol), "ok"),
    ]),
    h("div", { class: "card-body" }, [
      createProtocolSwitcher(),
      h("div", { class: "protocol-tip", text:
        protocolLabelLongOf(state.activeProtocol) + " —— " + (PROTOCOL_HINTS[state.activeProtocol] || "") }),
    ]),
  ]);

  /* ---- 下半部分：供应商列表 ---- */
  const listBody = h("div", { class: "card-body" });
  if (!state.suppliers.length) {
    listBody.appendChild(emptyState(
      "🔌",
      "还没有任何供应商",
      "一个供应商保存一份接口地址与密钥，可供多个协议使用。点击下面的按钮创建供应商。",
      [
        h("button", {
          class: "btn primary",
          type: "button",
          text: "➕ 新增供应商",
          onclick: () => addSupplier(),
        }),
      ],
    ));
  } else {
    const list = h("div", { class: "supplier-list" });
    state.suppliers.forEach((supplier, index) => {
      list.appendChild(createSupplierCard(supplier, index));
    });
    listBody.appendChild(list);
    listBody.appendChild(h("div", { class: "row", style: "margin-top:12px" }, [
      h("button", {
        class: "btn primary",
        type: "button",
        text: "➕ 新增供应商",
        onclick: () => addSupplier(),
      }),
      h("span", { class: "hint", text: "新增后请填写名称、Base URL 与 API Key，再点右上角「💾 保存配置」。" }),
    ]));
  }

  const supplierCard = h("div", { class: "card" }, [
    h("div", { class: "card-head" }, [
      h("div", {}, [
        h("h3", { text: "🔌 供应商列表" }),
        h("div", { class: "hint", text: "可增删改、可设为「当前使用」、可测试连接与获取模型列表。" }),
      ]),
      h("div", { class: "row tight" }, [
        h("span", { class: "badge plain", id: "supplier-count-badge", text: state.suppliers.length + " 个供应商" }),
        h("span", { class: "badge ok", id: "supplier-active-badge", text: "当前：" + (activeSupplierName() || "未选择") }),
      ]),
    ]),
    listBody,
  ]);

  /* ---- 模型设置 ---- */
  const modelCard = h("div", { class: "card" }, [
    h("div", { class: "card-head" }, [
      h("h3", { text: "🧠 模型设置" }),
      h("span", { class: "hint", text: "按协议分别保存，切换协议自动套用对应模型。" }),
    ]),
    h("div", { class: "card-body" }, [createModelSection()]),
  ]);

  host.appendChild(protocolCard);
  host.appendChild(supplierCard);
  host.appendChild(modelCard);
}

/** 新增一个供应商（默认填入当前协议的建议地址）。 */
function addSupplier() {
  const index = state.suppliers.length + 1;
  const protocol = state.activeProtocol;
  state.suppliers.push({
    name: "供应商 " + index,
    base_url: DEFAULT_BASE_URLS[protocol] || "",
    api_key: "",
    api_key_set: false,
    api_key_masked: "",
  });
  const cfg = state.config || {};
  if (!activeSupplierName()) cfg.active_supplier = "供应商 " + index;
  markDirty();
  renderSuppliers();
  renderOverview();
  paintSummary();
  toast("已新增供应商，请填写名称、Base URL 与 API Key 后保存", "info");
}

/* ---------------------------------------------------- 模型与尺寸 */

function renderModels() {
  const body = $("models-body");
  if (!body) return;
  clear(body);

  const cfg = state.config || {};
  const sizes = trimList(cfg.image_sizes);
  const activeSize = String(cfg.active_size || (sizes[0] || ""));

  /* ---- 当前尺寸 + 通用出图参数 ---- */
  const sizeSelect = h("select", {
    class: "select",
    id: "active-size",
    onchange: markDirty,
  }, (sizes.length ? sizes : [activeSize || ""]).filter(Boolean).map((size) =>
    h("option", { value: size, text: size, selected: size === activeSize })));

  const timeoutInput = h("input", {
    class: "input",
    id: "timeout",
    type: "number",
    inputmode: "numeric",
    min: "1",
    step: "1",
    value: String(num(cfg.timeout, 600)),
    oninput: markDirty,
  });

  const inputImages = h("input", {
    class: "input",
    id: "max-input-images",
    type: "number",
    inputmode: "numeric",
    min: "1",
    max: "20",
    step: "1",
    value: String(num(cfg.max_input_images, 6)),
    oninput: markDirty,
  });

  const concurrency = h("input", {
    class: "input",
    id: "max-concurrent",
    type: "number",
    inputmode: "numeric",
    min: "1",
    max: "16",
    step: "1",
    value: String(num(cfg.max_concurrent, 2)),
    oninput: markDirty,
  });

  const replyRef = h("input", {
    type: "checkbox",
    id: "reply-reference-image",
    checked: cfg.reply_reference_image !== false,
    onchange: markDirty,
  });
  const batchInput = h("input", { class: "input", id: "batch-max", type: "number", min: "1", max: "10", step: "1", value: String(clampInt(cfg.batch_max, 1, 10, 4)), oninput: markDirty });
  const autoSize = h("input", { type: "checkbox", id: "size-auto-detect", checked: cfg.size_auto_detect !== false, onchange: markDirty });
  const quality = h("select", { class: "select", id: "quality", onchange: markDirty },
    ["auto", "low", "medium", "high", "xhigh", "max"].map((value) => h("option", { value, text: value, selected: value === String(cfg.quality || "auto") })));
  const transparent = h("input", { type: "checkbox", id: "transparent-background", checked: cfg.transparent_background === true, onchange: markDirty });
  const qqFileFallback = h("input", { type: "checkbox", id: "qq-file-fallback", checked: cfg.qq_file_fallback !== false, onchange: markDirty });

  body.appendChild(
    h("div", { class: "grid" }, [
      textField("当前图片尺寸", sizeSelect, "群里可让主人用「尺寸列表」「切换尺寸」操作。"),
      textField("绘画接口超时时间（秒）", timeoutInput, "默认 600 秒。生成大图或使用较慢的中转站时可以调大。"),
      textField("最大输入图片数", inputImages, "单次编辑 / 多图编辑允许的最大图片数量（1 ~ 20，默认 6）。"),
      textField("最大并发数", concurrency, "同时进行的生成任务上限（1 ~ 16，默认 2）。"),
      textField("批量出图上限", batchInput, "单条指令最多生成 10 张，默认 4 张。"),
      textField("生成质量", quality, "OpenAI 兼容协议支持 auto / low / medium / high / xhigh / max；其他协议会忽略此字段。"),
      h("div", { class: "field" }, [h("span", { class: "field-label", text: "透明背景" }), h("label", { class: "checkbox-row" }, [transparent, h("span", { text: "请求透明 PNG 背景（仅支持的协议生效）" })])]),
      h("div", { class: "field" }, [h("span", { class: "field-label", text: "QQ 图片发送回退" }), h("label", { class: "checkbox-row" }, [qqFileFallback, h("span", { text: "图片发送失败时尝试以文件形式发送（OneBot/QQ 官方适配器）" })])]),
      h("div", { class: "field" }, [h("span", { class: "field-label", text: "提示词尺寸识别" }), h("label", { class: "checkbox-row" }, [autoSize, h("span", { text: "提示词包含 16:9、1920x1080 等尺寸时自动识别" })])]),
      h("div", { class: "field" }, [
        h("label", { class: "field-label", text: "引用图片参与编辑" }),
        h("label", { class: "checkbox-row" }, [
          replyRef,
          h("span", { text: "开启后，引用一条含图片的消息再发送编辑指令，会自动取出被引用的图片参与编辑。" }),
        ]),
        hint("关闭后只能通过图片链接或直接发送图片来编辑。"),
      ]),
    ]),
  );

  /* ---- 尺寸管理 ---- */
  body.appendChild(h("hr", { class: "divider" }));
  body.appendChild(h("div", { class: "sub-title", text: "🖼 尺寸管理" }));
  body.appendChild(h("div", { class: "inline-note", text:
    "尺寸写法为「宽x高」（如 1024x1024）或 auto。回车添加、点 × 删除，增删会立即写入配置。" +
    "群里可让主人用「尺寸列表」查看、用「切换尺寸」切换。" }));

  const sizeResult = h("div", { class: "result" });
  const showSizeResult = (message, kind) => {
    state.sizeResult = message ? { message: message, kind: kind || "info" } : null;
    setResult(sizeResult, message, kind);
  };
  if (state.sizeResult) setResult(sizeResult, state.sizeResult.message, state.sizeResult.kind);

  const sizeChips = h("div", { class: "chips" });
  const sizeEditorBox = h("div", { class: "tag-editor" }, [sizeChips]);
  const sizeInput = h("input", {
    class: "tag-input",
    type: "text",
    placeholder: "输入尺寸后回车添加，例如 1024x1024",
  });

  const normalizeSize = (raw) =>
    String(raw || "").trim().toLowerCase().replace(/\s+/g, "").replace("×", "x").replace("*", "x");

  const refreshSizeDependent = () => {
    renderModels();
    paintSummary();
  };

  const addValue = async (raw) => {
    const size = normalizeSize(raw);
    if (!size) return false;
    if (!looksLikeSize(size)) {
      showSizeResult("❌ 尺寸格式不正确，应为「宽x高」（如 1024x1024）或 auto", "err");
      toast("尺寸格式不正确：" + size, "err");
      return false;
    }
    if (trimList(state.config.image_sizes).indexOf(size) !== -1) {
      showSizeResult("ℹ️ 该尺寸已存在：" + size, "info");
      return false;
    }
    showSizeResult("正在添加尺寸 " + size + " …", "info");
    try {
      const data = await apiPost("sizes/add", { size });
      state.config.image_sizes = trimList((data && data.sizes) || []);
      state.config.size_list_for = (await apiGet("config")).size_list_for;
      if (data && data.active) state.config.active_size = data.active;
      toast("已添加尺寸 " + size, "ok");
      refreshSizeDependent();
      return true;
    } catch (error) {
      showSizeResult("❌ 添加失败：" + errText(error), "err");
      return false;
    }
  };

  const removeValue = async (raw) => {
    const size = normalizeSize(raw);
    if (!size) return;
    const ok = await confirmDialog({
      title: "删除尺寸",
      message: "即将删除尺寸「" + size + "」。",
      danger: "如果它是当前使用的尺寸，插件会自动切换到列表中的第一个尺寸；删除会立即写入配置。",
      confirmText: "确认删除",
    });
    if (!ok) return;
    showSizeResult("正在删除尺寸 " + size + " …", "info");
    try {
      const data = await apiPost("sizes/delete", { size });
      state.config.image_sizes = trimList((data && data.sizes) || []);
      state.config.size_list_for = (await apiGet("config")).size_list_for;
      if (data && data.active) state.config.active_size = data.active;
      toast("已删除尺寸 " + size, "ok");
      refreshSizeDependent();
    } catch (error) {
      showSizeResult("❌ 删除失败：" + errText(error), "err");
    }
  };

  const commitSize = async (raw) => {
    const parts = String(raw || "")
      .split(/[,，;\n]/)
      .map((item) => item.trim())
      .filter(Boolean);
    let added = false;
    for (let index = 0; index < parts.length; index += 1) {
      if (await addValue(parts[index])) added = true;
    }
    if (!added && !parts.length) showSizeResult("请输入尺寸，例如 1024x1024 或 auto", "info");
  };

  sizeInput.addEventListener("keydown", async (event) => {
    if (event.key !== "Enter" && event.key !== "," && event.key !== "，" && event.key !== ";") return;
    event.preventDefault();
    const raw = sizeInput.value;
    sizeInput.value = "";
    await commitSize(raw);
  });
  sizeInput.addEventListener("blur", async () => {
    if (!sizeInput.value.trim()) return;
    const raw = sizeInput.value;
    sizeInput.value = "";
    await commitSize(raw);
  });

  const paintSizeChips = () => {
    clear(sizeChips);
    const list = trimList(state.config.image_sizes);
    if (!list.length) {
      sizeChips.appendChild(h("span", { class: "hint", text: "暂无尺寸，请添加" }));
      return;
    }
    list.forEach((size) => {
      sizeChips.appendChild(
        h("span", { class: "chip" + (size === activeSize ? " active" : "") }, [
          h("span", { text: size }),
          h("button", {
            class: "chip-x",
            type: "button",
            title: "删除该尺寸" + (size === activeSize ? "（当前使用中）" : ""),
            text: "×",
            onclick: () => removeValue(size),
          }),
        ]),
      );
    });
  };

  sizeChips.addEventListener("click", () => sizeInput.focus());

  const presetRow = h("div", { class: "row tight" }, SIZE_PRESETS.map((size) =>
    h("button", {
      class: "btn tiny",
      type: "button",
      text: size,
      title: "点击添加该尺寸",
      onclick: async (event) => {
        const button = event.currentTarget;
        setBusy(button, true, "添加中…");
        await addValue(size);
        setBusy(button, false);
      },
    })));

  body.appendChild(h("div", { class: "row", style: "margin-top:10px" }, [sizeInput]));
  body.appendChild(h("div", { class: "hint", style: "margin-top:10px", text: "常用尺寸快捷添加：" }));
  body.appendChild(presetRow);
  body.appendChild(h("div", { class: "sub-block" }, [
    h("div", { class: "hint", text: "已配置尺寸（点 × 删除，输入框回车添加，均会立即写入配置）：" }),
    sizeEditorBox,
    sizeResult,
  ]));

  paintSizeChips();
}

/* ---------------------------------------------------- 指令设置 */

function renderCommands() {
  const body = $("commands-body");
  if (!body) return;
  clear(body);

  const cfg = state.config || {};
  const editors = {};

  const fields = [
    { key: "draw_commands", label: "🖌 绘画指令", defaults: DEFAULT_COMMANDS.draw, hint: "「指令 + 内容」触发生成图片，例如：绘画广州塔宣传图" },
    { key: "edit_commands", label: "🖼 图片编辑指令", defaults: DEFAULT_COMMANDS.edit, hint: "「指令 + 内容 + 图片」触发编辑；也用于引用编辑、多图编辑与 @头像编辑" },
    { key: "menu_commands", label: "📋 菜单指令", defaults: DEFAULT_COMMANDS.menu, hint: "查看插件菜单，例如：菜单 / 绘画菜单" },
    { key: "master_commands", label: "👑 主人指令", defaults: DEFAULT_COMMANDS.master, hint: "群开关、切换供应商 / 协议 / 模型 / 尺寸、增删尺寸、增删主人、重载配置、运行统计、重置设置等仅主人可用的指令名" },
  ];

  fields.forEach((field) => {
    const values = trimList(cfg[field.key]);
    const editor = createTagEditor({
      values: values.length ? values : field.defaults.slice(),
      defaults: field.defaults,
      placeholder: "回车或逗号添加多个指令",
      onChange: () => markDirty(),
    });
    editors[field.key] = { editor, field };

    /* 已删除的默认指令：提供一键补回的胶囊，避免用户删完就找不回来。 */
    const restoreRow = h("div", { class: "row tight", style: "margin-top:8px" });
    const paintRestore = () => {
      clear(restoreRow);
      const current = editor.getValues();
      const missing = field.defaults.filter((item) => current.indexOf(item) === -1);
      if (!missing.length) return;
      restoreRow.appendChild(h("span", { class: "hint", text: "已移除的默认指令：" }));
      missing.forEach((item) => {
        restoreRow.appendChild(
          h("button", {
            class: "chip add",
            type: "button",
            text: "+ " + item,
            title: "点击把「" + item + "」加回指令列表",
            onclick: () => {
              if (editor.add(item)) {
                editor.emit();
                paintRestore();
                markDirty();
                toast("已加回默认指令：" + item + "（保存后生效）", "info");
              }
            },
          }),
        );
      });
    };

    const editorNode = createTagEditor ? editor.element : null;
    if (editor.element) editor.element.addEventListener("click", () => setTimeout(paintRestore, 0));
    editor.element.addEventListener("input", () => setTimeout(paintRestore, 0));
    editor.element.addEventListener("keyup", () => setTimeout(paintRestore, 0));

    body.appendChild(
      h("div", { class: "field", style: "margin-bottom:16px" }, [
        h("div", { class: "row tight" }, [
          iconTile("⌨️", "sm"),
          h("label", { class: "field-label", text: field.label }),
          editorNode ? null : null,
          h("span", { class: "spacer" }),
          h("button", {
            class: "btn tiny ghost",
            type: "button",
            text: "↩ 恢复默认",
            onclick: () => {
              editor.setValues(field.defaults.slice());
              paintRestore();
              markDirty();
              toast("已恢复「" + field.label + "」的默认指令（保存后生效）", "info");
            },
          }),
        ]),
        editor.element,
        restoreRow,
        h("div", { class: "hint", text: field.hint + "（默认：" + field.defaults.join(" / ") + "）" }),
      ]),
    );
    paintRestore();
  });

  /* ---- 供应商 / 协议 / 统计类指令（8 项，顺序固定） ---- */
  body.appendChild(h("hr", { class: "divider" }));
  body.appendChild(h("div", { class: "sub-title", text: "🔌 供应商 / 协议 / 统计类指令" }));
  body.appendChild(h("div", { class: "inline-note", text:
    "以下 8 个指令按固定顺序对应插件功能，可改名但请不要增删（必须保持 8 项且都不能为空）。" +
    "「切换供应商 / 切换协议 / 切换模型 / 运行统计 / 重置设置」需要主人权限。" }));

  const supplierCommandEditors = [];
  const supplierDefaults = trimList(cfg.supplier_commands);
  SUPPLIER_COMMAND_FIELDS.forEach((field, index) => {
    const value = supplierDefaults[index] || field.label;
    const input = h("input", {
      class: "input",
      type: "text",
      value,
      placeholder: field.label,
      oninput: () => markDirty(),
    });
    supplierCommandEditors.push(input);
    body.appendChild(
      h("div", { class: "field", style: "margin-top:12px" }, [
        h("div", { class: "row tight" }, [
          h("label", { class: "field-label", text: (index + 1) + ". " + field.label }),
          badge(field.hint, "plain"),
          h("span", { class: "spacer" }),
          h("button", {
            class: "btn tiny ghost",
            type: "button",
            text: "↩ 恢复默认",
            onclick: () => {
              input.value = field.label;
              markDirty();
              toast("已恢复默认指令：" + field.label + "（保存后生效）", "info");
            },
          }),
        ]),
        input,
      ]),
    );
  });

  editors.supplier_commands = { inputs: supplierCommandEditors };
  state.commandEditors = editors;
  body.appendChild(createCollectionEditor("menu_sections", "菜单分组", 20));
}

/* ---------------------------------------------------- 文案设置 */

function renderPrompts() {
  const body = $("prompts-body");
  if (!body) return;
  clear(body);

  const cfg = state.config || {};
  const textareas = {};

  const layout = h("div", { class: "grid", style: "grid-template-columns: minmax(0, 2fr) minmax(230px, 1fr)" });

  const left = h("div", {});
  const right = h("div", {});

  /* 记录最后聚焦的文本框，点变量时插入到那个框（默认开始绘画文案）。 */
  let lastAreaKey = "start_prompt";

  const items = [
    { key: "start_prompt", label: "⏳ 开始绘画文案", hint: "收到请求后立即回复。可用 {prompt_type} 自动区分绘画 / 图片编辑。" },
    { key: "done_prompt", label: "✅ 完成文案", hint: "出图完成后回复，{耗时} 已保留两位小数。" },
    { key: "menu_text", label: "📋 菜单文案", hint: "发送菜单指令时回复的内容，支持 {供应商} / {协议} / {群状态} 等变量。" },
  ];

  items.forEach((item) => {
    const fallback = DEFAULT_PROMPTS[item.key] || "";
    const value = cfg[item.key] === undefined || cfg[item.key] === null || cfg[item.key] === "" ? fallback : String(cfg[item.key]);
    const area = h("textarea", {
      class: "textarea",
      rows: item.key === "menu_text" ? 14 : 6,
      value,
      "aria-label": item.label,
      onfocus: () => {
        lastAreaKey = item.key;
      },
      oninput: () => markDirty(),
    });
    textareas[item.key] = area;
    left.appendChild(
      h("div", { class: "field", style: "margin-bottom:16px" }, [
        h("div", { class: "row tight" }, [
          h("label", { class: "field-label", text: item.label }),
          h("span", { class: "spacer" }),
          h("button", {
            class: "btn tiny ghost",
            type: "button",
            text: "↩ 恢复默认",
            onclick: () => {
              area.value = fallback;
              markDirty();
              toast("已恢复默认文案，保存后生效", "info");
            },
          }),
        ]),
        area,
        h("div", { class: "hint", text: item.hint }),
      ]),
    );
  });

  left.appendChild(
    h("div", { class: "row tight" }, [
      h("button", {
        class: "btn",
        type: "button",
        text: "↩️ 恢复默认文案",
        onclick: () => {
          Object.keys(textareas).forEach((key) => {
            textareas[key].value = DEFAULT_PROMPTS[key] || "";
          });
          markDirty();
          toast("三个文案均已恢复默认，保存后生效", "info");
        },
      }),
      h("span", { class: "hint", text: "留空时插件也会自动回退到默认文案；点右侧变量会插入到当前聚焦的输入框。" }),
    ]),
  );

  right.appendChild(h("div", { class: "sub-title", text: "🧩 可用变量" }));
  const variables = PROMPT_VARIABLES.concat(EXTRA_PROMPT_VARIABLES);
  const variableSearch = h("input", { class: "input", type: "search", placeholder: "搜索变量名称或说明…", "aria-label": "搜索变量" });
  right.appendChild(h("div", { class: "hint", text: "点击变量会插入到当前光标位置；未聚焦时默认插入「开始绘画文案」。" }));
  right.appendChild(variableSearch);
  const varBox = h("div", { class: "vars" });
  const paintVariables = () => {
    clear(varBox);
    const needle = variableSearch.value.trim().toLowerCase();
    variables.filter((item) => !needle || (item.name + " " + item.desc).toLowerCase().includes(needle)).forEach((item) => {
      varBox.appendChild(h("button", {
        class: "chip",
        type: "button",
        title: item.desc,
        text: item.name,
        onclick: () => {
          const area = textareas[lastAreaKey] || textareas.start_prompt;
          if (!area) return;
          insertAtCaret(area, item.name);
          markDirty();
        },
      }));
    });
    if (!varBox.childNodes.length) varBox.appendChild(h("span", { class: "hint", text: "没有匹配的变量" }));
  };
  variableSearch.addEventListener("input", paintVariables);
  paintVariables();
  right.appendChild(varBox);

  right.appendChild(h("div", { class: "sub-title", style: "margin-top:16px", text: "📌 变量含义" }));
  const table = h("div", { class: "kv" });
  PROMPT_VARIABLES.forEach((item) => {
    table.appendChild(h("div", { class: "kv-key mono", text: item.name }));
    table.appendChild(h("div", { class: "kv-value", text: item.desc }));
  });
  right.appendChild(table);

  right.appendChild(h("div", { class: "sub-title", style: "margin-top:16px", text: "➕ 更多变量" }));
  const extraTable = h("div", { class: "kv" });
  EXTRA_PROMPT_VARIABLES.forEach((item) => {
    extraTable.appendChild(h("div", { class: "kv-key mono", text: item.name }));
    extraTable.appendChild(h("div", { class: "kv-value", text: item.desc }));
  });
  right.appendChild(extraTable);

  layout.appendChild(left);
  layout.appendChild(right);
  body.appendChild(layout);

  state.promptTextareas = textareas;
  body.appendChild(createCollectionEditor("prompts", "提示词库", 200));
  body.appendChild(createCollectionEditor("gacha_styles", "抽卡画风池", 200));
  const flavor = h("details", { class: "collection-panel", id: "flavor-lines-editor" }, [
    h("summary", { text: "出图文案池" }),
    editField("出图文案（每行一条）", trimList(cfg.flavor_lines).join("\n"), (value) => {
      cfg.flavor_lines = value.split("\n").map((line) => line.trim()).filter(Boolean);
    }, true),
    h("button", { type: "button", class: "btn tiny", text: "恢复出厂文案", onclick: () => {
      cfg.flavor_lines = cloneDefault("flavor_lines");
      flavor.querySelector("textarea").value = cfg.flavor_lines.join("\n");
      markDirty();
    } }),
  ]);
  body.appendChild(flavor);
}

/* ------------------------------------------------ 权限与范围：小组件 */

function createPlatformCheckbox(item, checkedSet, onChange) {
  const input = h("input", {
    type: "checkbox",
    checked: checkedSet.has(item.id),
    onchange: () => {
      if (input.checked) checkedSet.add(item.id);
      else checkedSet.delete(item.id);
      if (onChange) onChange();
    },
  });
  return h("label", { class: "checkbox-item" }, [
    input,
    h("span", { class: "ci-main" }, [
      h("span", { class: "ci-title", text: item.name || item.id }),
      item.desc ? h("div", { class: "hint", text: item.desc }) : null,
      h("div", { class: "hint mono", text: item.id }),
    ]),
  ]);
}

function createBotCheckbox(item, checkedSet, onChange) {
  const input = h("input", {
    type: "checkbox",
    checked: checkedSet.has(item.id),
    onchange: () => {
      if (input.checked) checkedSet.add(item.id);
      else checkedSet.delete(item.id);
      if (onChange) onChange();
    },
  });
  const parts = [];
  parts.push(h("span", { class: "ci-title", text: item.display_name || item.name || item.id }));
  if (item.name && item.name !== item.display_name) {
    parts.push(h("div", { class: "hint", text: "适配器类型：" + item.name }));
  }
  if (item.description) parts.push(h("div", { class: "hint", text: item.description }));
  parts.push(h("div", { class: "hint mono", text: "实例 ID：" + item.id }));
  return h("label", { class: "checkbox-item" }, [input, h("span", { class: "ci-main" }, parts)]);
}

function renderAccess() {
  const body = $("access-body");
  if (!body) return;
  clear(body);

  const cfg = state.config || {};
  const masterSet = new Set(trimList(cfg.masters));
  const platformSet = new Set(trimList(cfg.enabled_platforms));
  const botSet = new Set(trimList(cfg.enabled_bot_ids));

  /* 顶部统计条：一眼看清「谁能用、在哪些平台生效」。 */
  const summaryStrip = h("div", { class: "summary-strip" }, [
    pill("主人 " + masterSet.size + " 人", masterSet.size ? "ok" : "warn",
      masterSet.size ? "已配置 " + masterSet.size + " 位主人" : "还没配置主人，管理指令只能由私聊里的 AstrBot 管理员使用"),
    pill(
      platformSet.size ? "生效平台 " + platformSet.size + " 种" : "全部平台生效",
      platformSet.size ? "ok" : "plain",
      platformSet.size ? Array.from(platformSet).join("、") : "留空表示所有平台都生效",
    ),
    pill(
      botSet.size ? "生效实例 " + botSet.size + " 个" : "该平台全部实例",
      botSet.size ? "ok" : "plain",
      botSet.size ? Array.from(botSet).join("、") : "留空表示该平台下所有机器人都生效",
    ),
    pill(
      cfg.group_mode === "whitelist" ? "白名单群 " + trimList(cfg.group_list).length + " 个"
        : (cfg.group_mode === "blacklist" ? "黑名单群 " + trimList(cfg.group_list).length + " 个" : "全部群聊生效"),
      cfg.group_mode === "all" ? "plain" : "ok",
      "群聊策略可在下方「群聊与私聊」中调整",
    ),
  ]);

  body.appendChild(summaryStrip);

  /* ---- 触发方式（trigger_mode） ---- */
  let triggerMode = ["at", "command"].indexOf(String(cfg.trigger_mode || "at")) === -1 ? "at" : String(cfg.trigger_mode || "at");
  const triggerHost = h("div", { class: "protocol-tabs" });

  const paintTrigger = () => {
    clear(triggerHost);
    TRIGGER_MODE_OPTIONS.forEach((option) => {
      const active = option.value === triggerMode;
      triggerHost.appendChild(
        h("button", {
          class: "protocol-tab" + (active ? " active" : ""),
          type: "button",
          role: "radio",
          "aria-checked": active ? "true" : "false",
          onclick: () => {
            triggerMode = option.value;
            markDirty();
            paintTrigger();
            toast("已切换触发方式：" + option.title + "（保存后生效）", "info");
          },
        }, [
          h("span", { class: "protocol-radio", "aria-hidden": "true" }),
          h("span", { class: "protocol-main" }, [
            h("span", { class: "protocol-name" }, [
              h("span", { text: option.title }),
              option.badge ? badge(option.badge, "ok") : null,
              active ? badge("当前", "plain") : null,
            ]),
            h("div", { class: "protocol-form", text: option.desc }),
            option.warn ? h("div", { class: "inline-note warn", style: "margin-top:6px", text: option.warn }) : null,
          ]),
        ]),
      );
    });
  };
  paintTrigger();

  const triggerBox = h("div", { class: "card flat" }, [
    h("div", { class: "card-head" }, [
      iconTile("🎯", "sm"),
      h("h3", { text: "🎯 触发方式" }),
    ]),
    h("div", { class: "card-body" }, [
      triggerHost,
      h("div", { class: "hint", style: "margin-top:8px", text:
        "QQ 官方机器人（qq_official / qq_official_webhook）在群聊里默认免 @ 响应，无需额外设置；" +
        "OneBot v11 默认需要 @机器人 或唤醒前缀。「不需要 @」只对群聊生效，私聊仍然直接发送指令即可。" +
        "选择「不需要 @」时建议配合群白名单，避免被刷屏。" }),
    ]),
  ]);

  /* ---- 稳定性：冷却 / 重试 / 代理 ---- */
  const cooldownInput = h("input", {
    class: "input",
    id: "cooldown",
    type: "number",
    inputmode: "numeric",
    min: "0",
    step: "1",
    value: String(Math.max(0, Math.round(num(cfg.cooldown, 0)))),
    oninput: markDirty,
  });

  const retryInput = h("input", {
    class: "input",
    id: "retry-times",
    type: "number",
    inputmode: "numeric",
    min: "0",
    max: "3",
    step: "1",
    value: String(clampInt(cfg.retry_times, 0, 3, 1)),
    oninput: markDirty,
  });

  const proxyInput = h("input", {
    class: "input",
    id: "proxy",
    type: "text",
    value: String(cfg.proxy || ""),
    placeholder: "http://127.0.0.1:7890（留空不用）",
    oninput: markDirty,
  });

  const stabilityBox = h("div", { class: "card flat" }, [
    h("div", { class: "card-head" }, [
      iconTile("🛡", "sm"),
      h("h3", { text: "🛡 稳定性与网络" }),
    ]),
    h("div", { class: "card-body" }, [
      h("div", { class: "grid" }, [
        textField("同用户冷却（秒）", cooldownInput, "0 表示不限制。冷却期间重复触发会提示剩余时间。"),
        textField("失败自动重试次数", retryInput, "针对超时 / 网络错误 / 5xx 自动重试，范围 0 ~ 3，0 表示不重试。"),
        textField("HTTP 代理（可选）", proxyInput, "形如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080，留空表示不使用。"),
      ]),
    ]),
  ]);

  /* ---- 主人设置 ---- */
  const mastersEditor = createTagEditor({
    values: Array.from(masterSet),
    placeholder: "输入用户 ID 后回车添加",
    onChange: () => markDirty(),
    onRemove: () => markDirty(),
  });

  const masterIdInput = h("input", { class: "input", type: "text", inputmode: "numeric", placeholder: "输入主人用户 ID" });
  const addMasterBtn = h("button", {
    class: "btn",
    type: "button",
    text: "➕ 添加主人",
    onclick: () => {
      const value = masterIdInput.value.trim();
      if (!value) {
        toast("请先填写用户 ID", "err");
        return;
      }
      const before = mastersEditor.getValues().length;
      mastersEditor.commit(value);
      masterIdInput.value = "";
      if (mastersEditor.getValues().length > before) {
        markDirty();
        toast("已添加主人：" + value + "（保存后生效）", "ok");
      } else {
        toast("该 ID 已在主人列表中", "warn");
      }
    },
  });

  const adminToggle = h("input", {
    type: "checkbox",
    id: "masters-use-admin",
    checked: cfg.masters_use_astrbot_admin !== false,
    onchange: () => markDirty(),
  });

  const recentUserList = createRecentList({
    idLabel: "用户 ID",
    emptyText: "还没有记录到用户，先让目标用户 @机器人 发送一次指令（例如「我的ID」）后再试",
    titleOf: (item) => String(item.name || "").trim() || "用户 " + String(item.id).trim(),
    commit: (id) => {
      const before = mastersEditor.getValues().length;
      mastersEditor.commit(id);
      return mastersEditor.getValues().length > before;
    },
    addedText: (id) => "已添加主人：" + id + "（保存后生效）",
    dupHint: "该 ID 已在主人列表中",
  });
  const recentUserResult = h("div", { class: "result" });

  const masterCard = h("div", { class: "card flat" }, [
    h("div", { class: "card-head" }, [h("h3", { text: "👑 主人设置" })]),
    h("div", { class: "card-body" }, [
      h("div", { class: "hint", text: "主人可以使用群开关、切换供应商 / 协议 / 模型 / 尺寸、增删尺寸、增删主人、重载配置、运行统计、重置设置等指令。" }),
      h("div", { class: "row", style: "margin-top:8px" }, [masterIdInput, addMasterBtn]),
      h("div", { class: "row", style: "margin-top:8px" }, [
        h("button", {
          class: "btn",
          type: "button",
          text: "🔍 一键获取最近用户",
          onclick: async (event) => {
            const button = event.currentTarget;
            setBusy(button, true, "获取中…");
            setResult(recentUserResult, "正在读取插件最近记录到的用户…", "info");
            try {
              const data = await fetchRecent();
              const count = recentUserList.paint(data.users);
              setResult(
                recentUserResult,
                count
                  ? "✅ 获取到 " + count + " 个最近交互的用户，点击「➕ 添加」即可加入主人列表"
                  : "⚠️ 还没有记录到用户：先让目标用户在群里 @机器人 发送一次「我的ID」",
                count ? "ok" : "err",
              );
            } catch (error) {
              setResult(recentUserResult, "❌ 获取失败：" + errText(error), "err");
            } finally {
              setBusy(button, false);
            }
          },
        }),
        h("span", { class: "hint", text: "来自插件运行期记录，最多显示最近的 30 条。" }),
      ]),
      recentUserList.element,
      recentUserResult,
      h("div", { class: "inline-note", style: "margin-top:8px", text:
        "怎么获取自己的 ID？在群里 @机器人 发送「我的ID」，机器人会回复你的用户 ID / 群 ID / 平台 / 机器人实例 ID；" +
        "也可以点上面的「🔍 一键获取最近用户」，或打开 AstrBot 日志查看发送者 ID。" }),
      h("div", { class: "sub-block" }, [
        h("div", { class: "hint", text: "当前主人列表：" }),
        mastersEditor.element,
        h("label", { class: "checkbox-row", style: "margin-top:10px" }, [
          adminToggle,
          h("span", { text: "AstrBot 管理员自动视为主人（推荐开启）" }),
        ]),
      ]),
    ]),
  ]);

  /* ---- 平台类型 ---- */
  const typeResult = h("div", { class: "result" });
  const platformListHost = h("div", { class: "checkbox-list" });
  const customTypeInput = h("input", { class: "input", type: "text", placeholder: "自定义平台类型名，例如 telegram" });

  const paintPlatforms = () => {
    clear(platformListHost);
    const items = PLATFORM_PRESETS.slice();
    state.platformTypes.forEach((type) => {
      const name = String(type);
      if (!items.some((item) => item.id === name)) {
        items.push({ id: name, name: name, desc: "从 AstrBot 平台列表获取" });
      }
    });
    Array.from(platformSet).forEach((name) => {
      if (!items.some((item) => item.id === name)) {
        items.push({ id: name, name: name, desc: "自定义平台类型" });
      }
    });
    items.forEach((item) => {
      platformListHost.appendChild(createPlatformCheckbox(item, platformSet, () => {
        markDirty();
        paintPlatformExtra();
      }));
    });
  };

  const extraTypesHost = h("div", { class: "vars", style: "margin-top:8px" });
  const paintPlatformExtra = () => {
    clear(extraTypesHost);
    PLATFORM_PRESETS.forEach((item) => {
      if (platformSet.has(item.id)) return;
      extraTypesHost.appendChild(h("button", {
        class: "chip",
        type: "button",
        text: "+ " + item.id,
        onclick: () => {
          platformSet.add(item.id);
          markDirty();
          paintPlatforms();
          paintPlatformExtra();
        },
      }));
    });
  };

  const platformBox = h("div", {}, [
    h("div", { class: "row tight" }, [
      h("button", {
        class: "btn tiny",
        type: "button",
        text: "🔍 一键获取",
        onclick: async (event) => {
          const button = event.currentTarget;
          setBusy(button, true, "获取中…");
          setResult(typeResult, "正在读取 AstrBot 已加载的平台适配器…", "info");
          try {
            const data = await apiGet("platforms");
            state.platformTypes = trimList((data && data.types) || []);
            state.platformInstances = toArray(data && data.platforms);
            paintPlatforms();
            paintPlatformExtra();
            paintBots();
            renderBotOverrides();
            setResult(
              typeResult,
              "✅ 获取到 " + state.platformTypes.length + " 种平台类型、" + state.platformInstances.length + " 个机器人实例，请勾选后保存",
              "ok",
            );
          } catch (error) {
            setResult(typeResult, "❌ 获取失败：" + errText(error), "err");
          } finally {
            setBusy(button, false);
          }
        },
      }),
      h("span", { class: "hint", text: "留空 = 全部平台类型生效。" }),
    ]),
    platformListHost,
    extraTypesHost,
    h("div", { class: "row", style: "margin-top:8px" }, [
      customTypeInput,
      h("button", {
        class: "btn",
        type: "button",
        text: "➕ 添加自定义类型",
        onclick: () => {
          const value = customTypeInput.value.trim();
          if (!value) {
            toast("请先填写平台类型名", "err");
            return;
          }
          platformSet.add(value);
          customTypeInput.value = "";
          markDirty();
          paintPlatforms();
          paintPlatformExtra();
          toast("已添加平台类型：" + value + "（保存后生效）", "ok");
        },
      }),
    ]),
    typeResult,
  ]);

  /* ---- 机器人实例 ---- */
  const botResult = h("div", { class: "result" });
  const botListHost = h("div", { class: "checkbox-list" });
  const botManualEditor = createTagEditor({
    values: Array.from(botSet),
    placeholder: "输入机器人实例 ID 后回车添加",
    onChange: () => markDirty(),
    onRemove: () => markDirty(),
  });

  const paintBots = () => {
    clear(botListHost);
    if (!state.platformInstances.length) {
      botListHost.appendChild(h("div", { class: "inline-note", text: "还没有获取到实例列表，点击下方「🔍 一键获取」读取 AstrBot 已加载的机器人实例。" }));
      return;
    }
    state.platformInstances.forEach((item) => {
      botListHost.appendChild(createBotCheckbox(item, botSet, () => {
        botManualEditor.setValues(Array.from(botSet));
        markDirty();
      }));
    });
  };

  const botBox = h("div", {}, [
    h("div", { class: "row tight" }, [
      h("button", {
        class: "btn tiny",
        type: "button",
        text: "🔍 一键获取",
        onclick: async (event) => {
          const button = event.currentTarget;
          setBusy(button, true, "获取中…");
          setResult(botResult, "正在读取机器人实例…", "info");
          try {
            const data = await apiGet("platforms");
            state.platformTypes = trimList((data && data.types) || []);
            state.platformInstances = toArray(data && data.platforms);
            paintPlatforms();
            paintBots();
            setResult(botResult, "✅ 获取到 " + state.platformInstances.length + " 个机器人实例，勾选后保存即可限定生效范围", "ok");
          } catch (error) {
            setResult(botResult, "❌ 获取失败：" + errText(error), "err");
          } finally {
            setBusy(button, false);
          }
        },
      }),
      h("span", { class: "hint", text: "留空 = 全部机器人实例生效；多个机器人共用一个插件时，可只勾选其中一个。" }),
    ]),
    botListHost,
    h("div", { class: "sub-block" }, [
      h("div", { class: "hint", text: "手动填写实例 ID（OneBot v11 通常是机器人 QQ 号 / 平台实例名，QQ 官方机器人通常是 qq_official 等适配器实例 ID）：" }),
      botManualEditor.element,
    ]),
    botResult,
  ]);

  /* ---- 群聊与私聊 ---- */
  const privateToggle = h("input", {
    type: "checkbox",
    id: "private-enabled",
    checked: cfg.private_enabled !== false,
    onchange: () => markDirty(),
  });

  const groupModeValue = ["all", "whitelist", "blacklist"].indexOf(String(cfg.group_mode || "all")) === -1
    ? "all"
    : String(cfg.group_mode);
  const groupModeSelect = h("select", { class: "select", id: "group-mode", onchange: () => markDirty() },
    GROUP_MODE_OPTIONS.map((item) => h("option", {
      value: item.value,
      text: item.label,
      selected: item.value === groupModeValue,
    })));
  groupModeSelect.value = groupModeValue;

  const groupListEditor = createTagEditor({
    values: trimList(cfg.group_list),
    placeholder: "输入群号后回车添加",
    onChange: () => markDirty(),
    onRemove: () => markDirty(),
  });

  const groupInput = h("input", { class: "input", type: "text", inputmode: "numeric", placeholder: "输入群号 / 群 OpenID" });

  const recentGroupList = createRecentList({
    idLabel: "群 ID",
    emptyText: "还没有记录到群，先在目标群里 @机器人 发送一次「我的ID」或「群开关」",
    titleOf: (item) => String(item.name || "").trim() || "群 " + String(item.id).trim(),
    commit: (id) => {
      const before = groupListEditor.getValues().length;
      groupListEditor.commit(id);
      return groupListEditor.getValues().length > before;
    },
    addedText: (id) => "已添加群：" + id + "（保存后生效）",
    dupHint: "该群已在列表中",
  });
  const recentGroupResult = h("div", { class: "result" });

  const groupBox = h("div", {}, [
    h("div", { class: "grid" }, [
      textField("群聊模式", groupModeSelect, GROUP_MODE_OPTIONS.map((item) => item.label + "：" + item.desc).join("；")),
      h("div", { class: "field" }, [
        h("label", { class: "field-label", text: "私聊" }),
        h("label", { class: "checkbox-row" }, [privateToggle, h("span", { text: "允许私聊使用绘画 / 编辑" })]),
        hint("关闭后仅群聊可用。"),
      ]),
    ]),
    h("div", { class: "sub-block" }, [
      h("div", { class: "sub-title", text: "群列表（白名单 / 黑名单）" }),
      h("div", { class: "inline-note", text:
        "如何获取群号：OneBot v11 是数字群号（例如 123456789），QQ 官方机器人没有数字群号、群聊使用 group_openid；" +
        "最可靠的方式是在目标群里 @机器人 发送「我的ID」，机器人会直接回复群 ID 与用户 ID；" +
        "也可以点下面的「🔍 一键获取最近群」从插件记录里挑选。" }),
      h("div", { class: "row", style: "margin-top:8px" }, [
        groupInput,
        h("button", {
          class: "btn",
          type: "button",
          text: "➕ 添加群号",
          onclick: () => {
            const value = groupInput.value.trim();
            if (!value) {
              toast("请先填写群号", "err");
              return;
            }
            const before = groupListEditor.getValues().length;
            groupListEditor.commit(value);
            groupInput.value = "";
            if (groupListEditor.getValues().length > before) {
              markDirty();
              toast("已添加群：" + value + "（保存后生效）", "ok");
            } else {
              toast("该群已在列表中", "warn");
            }
          },
        }),
      ]),
      h("div", { class: "row", style: "margin-top:8px" }, [
        h("button", {
          class: "btn",
          type: "button",
          text: "🔍 一键获取最近群",
          onclick: async (event) => {
            const button = event.currentTarget;
            setBusy(button, true, "获取中…");
            setResult(recentGroupResult, "正在读取插件最近记录到的群…", "info");
            try {
              const data = await fetchRecent();
              const count = recentGroupList.paint(data.groups);
              setResult(
                recentGroupResult,
                count
                  ? "✅ 获取到 " + count + " 个最近交互的群，点击「➕ 添加」即可加入群列表（仅在群模式为白名单 / 黑名单时生效）"
                  : "⚠️ 还没有记录到群：先在目标群里 @机器人 发送一次「我的ID」或「群开关」",
                count ? "ok" : "err",
              );
            } catch (error) {
              setResult(recentGroupResult, "❌ 获取失败：" + errText(error), "err");
            } finally {
              setBusy(button, false);
            }
          },
        }),
        h("span", { class: "hint", text: "来自插件运行期记录，最多显示最近的 30 条。" }),
      ]),
      recentGroupList.element,
      recentGroupResult,
      groupListEditor.element,
    ]),
  ]);

  body.appendChild(
    h("div", { class: "grid", style: "grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)" }, [
      masterCard,
      h("div", { class: "card flat" }, [
        h("div", { class: "card-head" }, [h("h3", { text: "🖥 生效平台类型" })]),
        h("div", { class: "card-body" }, [platformBox]),
      ]),
    ]),
  );

  body.appendChild(h("div", { style: "margin-top:16px" }, [
    h("div", { class: "grid", style: "grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)" }, [
      triggerBox,
      stabilityBox,
    ]),
  ]));

  body.appendChild(
    h("div", { class: "card flat", style: "margin-top:16px" }, [
      h("div", { class: "card-head" }, [h("h3", { text: "🤖 生效机器人实例 ID" })]),
      h("div", { class: "card-body" }, [botBox]),
    ]),
  );
  const groupRequire = h("input", { type: "checkbox", id: "group-require-enable", checked: cfg.group_require_enable !== false, onchange: markDirty });
  const blacklistEditor = createTagEditor({ values: trimList(cfg.user_blacklist), placeholder: "用户ID 或 平台ID|用户ID，回车添加", onChange: markDirty, onRemove: markDirty });
  body.appendChild(h("div", { class: "card flat", style: "margin-top:16px" }, [
    h("div", { class: "card-head" }, [h("h3", { text: "🛡 群聊保护" })]),
    h("div", { class: "card-body" }, [
      h("label", { class: "checkbox-row" }, [groupRequire, h("span", { text: "群聊需主人先开启（推荐）" })]),
      hint("开启后，主人需在目标群发送「开群」；黑名单用户始终不能使用插件。"),
      h("div", { class: "sub-title", text: "群友黑名单" }),
      blacklistEditor.element,
    ]),
  ]));

  body.appendChild(
    h("div", { class: "card flat", style: "margin-top:16px" }, [
      h("div", { class: "card-head" }, [h("h3", { text: "💬 群聊与私聊" })]),
      h("div", { class: "card-body" }, [groupBox]),
    ]),
  );

  paintPlatforms();
  paintPlatformExtra();
  paintBots();

  state.accessEditors = {
    mastersEditor,
    platformSet,
    botSet,
    botManualEditor,
    groupListEditor,
    blacklistEditor,
    groupRequire,
    mastersUseAdmin: adminToggle,
    privateToggle,
    groupModeSelect,
    getTriggerMode: () => triggerMode,
  };
  renderBotOverrides();
}

/* ------------------------------------------------------------ 收集与保存 */

function collectPayload() {
  const payload = {};
  const cfg = state.config || {};

  /* 供应商与协议 */
  syncModelInputsToState();
  payload.active_supplier = String(cfg.active_supplier || "");
  payload.active_protocol = String(state.activeProtocol || "openai");
  payload.suppliers = state.suppliers.map((item) => ({
    name: String(item.name || "").trim(),
    base_url: String(item.base_url || "").trim(),
    api_key: item.api_key ? String(item.api_key) : "",
    api_key_clear: item.api_key_clear === true,
  }));
  payload.protocol_models = PROTOCOL_KEYS.reduce((acc, key) => {
    const entry = currentProtocolModels(key);
    acc[key] = {
      model: String(entry.model || "").trim(),
      edit_model: String(entry.edit_model || "").trim(),
    };
    return acc;
  }, {});

  /* 模型与尺寸 */
  const sizeSelect = $("active-size");
  if (sizeSelect) payload.active_size = sizeSelect.value;
  const timeoutInput = $("timeout");
  if (timeoutInput) payload.timeout = num(timeoutInput.value, 600);
  const inputImages = $("max-input-images");
  if (inputImages) payload.max_input_images = clampInt(inputImages.value, 1, 20, 6);
  const concurrency = $("max-concurrent");
  if (concurrency) payload.max_concurrent = clampInt(concurrency.value, 1, 16, 2);
  const replyRef = $("reply-reference-image");
  if (replyRef) payload.reply_reference_image = Boolean(replyRef.checked);
  payload.image_sizes = trimList(cfg.image_sizes);
  payload.batch_max = Number(($("batch-max") || {}).value ?? cfg.batch_max ?? 4);
  payload.size_auto_detect = Boolean(($("size-auto-detect") || {}).checked ?? cfg.size_auto_detect);
  payload.quality = String(($('quality') || {}).value ?? cfg.quality ?? "auto");
  payload.transparent_background = Boolean(($('transparent-background') || {}).checked ?? cfg.transparent_background);
  payload.qq_file_fallback = Boolean(($('qq-file-fallback') || {}).checked ?? cfg.qq_file_fallback);
  payload.strict_trigger = Boolean(($("strict-trigger") || {}).checked ?? cfg.strict_trigger);
  payload.group_require_enable = Boolean(($("group-require-enable") || {}).checked ?? cfg.group_require_enable);
  payload.user_blacklist = trimList(cfg.user_blacklist);
  payload.bot_overrides = cfg.bot_overrides && typeof cfg.bot_overrides === "object" ? cfg.bot_overrides : {};
  payload.menu_sections = Array.isArray(cfg.menu_sections) ? cfg.menu_sections : [];
  payload.prompts = Array.isArray(cfg.prompts) ? cfg.prompts : [];
  payload.gacha_styles = Array.isArray(cfg.gacha_styles) ? cfg.gacha_styles : [];
  payload.gacha_enabled = Boolean(($("gacha-enabled") || {}).checked ?? cfg.gacha_enabled ?? true);
  payload.rank_enabled = Boolean(($("rank-enabled") || {}).checked ?? cfg.rank_enabled ?? true);
  payload.flavor_enabled = Boolean(($("flavor-enabled") || {}).checked ?? cfg.flavor_enabled ?? true);
  payload.flavor_lines = trimList(cfg.flavor_lines);
  payload.translate_enabled = Boolean(($("translate-enabled") || {}).checked ?? cfg.translate_enabled);
  payload.translate_model = String(($("translate-model") || {}).value ?? cfg.translate_model ?? "").trim();
  payload.translate_supplier = String(($("translate-supplier") || {}).value ?? cfg.translate_supplier ?? "").trim();
  payload.translate_prompt_text = String(($("translate-prompt-text") || {}).value ?? cfg.translate_prompt_text ?? "");
  payload.img2prompt_enabled = Boolean(($("img2prompt-enabled") || {}).checked ?? cfg.img2prompt_enabled);
  payload.img2prompt_model = String(($("img2prompt-model") || {}).value ?? cfg.img2prompt_model ?? "").trim();

  /* 指令 */
  const editors = state.commandEditors || {};
  ["draw_commands", "edit_commands", "menu_commands", "master_commands"].forEach((key) => {
    const entry = editors[key];
    if (entry && entry.editor) payload[key] = trimList(entry.editor.getValues());
  });
  if (editors.supplier_commands && editors.supplier_commands.inputs) {
    payload.supplier_commands = editors.supplier_commands.inputs.map((input) => String(input.value || "").trim());
  }

  /* 文案 */
  const areas = state.promptTextareas || {};
  ["start_prompt", "done_prompt", "menu_text"].forEach((key) => {
    const area = areas[key];
    if (area) payload[key] = String(area.value || "");
  });

  /* 权限与范围 */
  const access = state.accessEditors || {};
  if (access.mastersEditor) payload.masters = trimList(access.mastersEditor.getValues());
  if (access.mastersUseAdmin) payload.masters_use_astrbot_admin = Boolean(access.mastersUseAdmin.checked);
  if (access.platformSet) payload.enabled_platforms = Array.from(access.platformSet);
  if (access.botSet) {
    const botIds = Array.from(access.botSet);
    if (access.botManualEditor) {
      trimList(access.botManualEditor.getValues()).forEach((id) => {
        if (botIds.indexOf(id) === -1) botIds.push(id);
      });
    }
    payload.enabled_bot_ids = botIds;
  }
  if (access.privateToggle) payload.private_enabled = Boolean(access.privateToggle.checked);
  if (access.groupModeSelect) payload.group_mode = access.groupModeSelect.value;
  if (access.groupListEditor) payload.group_list = trimList(access.groupListEditor.getValues());
  if (access.blacklistEditor) payload.user_blacklist = trimList(access.blacklistEditor.getValues());
  if (access.groupRequire) payload.group_require_enable = Boolean(access.groupRequire.checked);
  if (typeof access.getTriggerMode === "function") payload.trigger_mode = access.getTriggerMode();

  /* 稳定性与网络 */
  /* 冷却与重试按原值上报，交给 validatePayload 明确报错，避免静默改写用户输入。 */
  const cooldownInput = $("cooldown");
  if (cooldownInput) payload.cooldown = Math.round(num(cooldownInput.value, 0));
  const retryInput = $("retry-times");
  if (retryInput) payload.retry_times = Math.round(num(retryInput.value, 1));
  const proxyInput = $("proxy");
  if (proxyInput) payload.proxy = String(proxyInput.value || "").trim();

  return payload;
}

function validatePayload(payload) {
  /* ---- 供应商 ---- */
  const suppliers = payload.suppliers || [];
  if (!suppliers.length) return "至少需要保留一个供应商";
  const names = [];
  for (let index = 0; index < suppliers.length; index += 1) {
    const item = suppliers[index];
    const label = "第 " + (index + 1) + " 个供应商";
    if (!item.name) return label + "的名称不能为空";
    if (names.indexOf(item.name) !== -1) return "供应商名称重复：" + item.name;
    names.push(item.name);
    if (!item.base_url) return "供应商「" + item.name + "」的 Base URL 不能为空";
    if (!looksLikeUrl(item.base_url, false)) {
      return "供应商「" + item.name + "」的 Base URL 必须以 http:// 或 https:// 开头";
    }
  }
  if (payload.active_supplier && names.indexOf(payload.active_supplier) === -1) {
    return "当前供应商「" + payload.active_supplier + "」不在供应商列表中，请重新选择";
  }
  if (!payload.active_supplier) return "请选择一个当前使用的供应商";

  /* ---- 协议 ---- */
  if (PROTOCOL_KEYS.indexOf(String(payload.active_protocol || "")) === -1) {
    return "请选择列表中的一个接口协议";
  }
  const protocolModels = payload.protocol_models || {};
  for (let index = 0; index < PROTOCOL_KEYS.length; index += 1) {
    const key = PROTOCOL_KEYS[index];
    const entry = protocolModels[key] || {};
    if (!entry.model && SELFHOST_PROTOCOLS.indexOf(key) === -1) return "协议「" + protocolShortLabelOf(key) + "」的生成模型不能为空";
  }

  /* ---- 超时与尺寸 ---- */
  if (!Number.isFinite(payload.timeout) || payload.timeout <= 0) return "接口超时时间必须是大于 0 的数字";
  const sizes = payload.image_sizes || [];
  if (!sizes.length) return "至少需要保留一个图片尺寸";
  const badSize = sizes.find((size) => !looksLikeSize(size));
  if (badSize) return "图片尺寸格式不正确：" + badSize + "（应形如 1024x1024 或 auto）";
  if (payload.active_size && sizes.indexOf(payload.active_size) === -1) {
    return "当前尺寸「" + payload.active_size + "」不在尺寸列表中";
  }

  /* ---- 指令 ---- */
  if (!(payload.draw_commands || []).length) return "绘画指令至少保留一条";
  if (!(payload.edit_commands || []).length) return "图片编辑指令至少保留一条";
  if (!(payload.menu_commands || []).length) return "菜单指令至少保留一条";
  if (!(payload.master_commands || []).length) return "主人指令至少保留一条";
  const supplierCommands = payload.supplier_commands || [];
  if (supplierCommands.length !== 8) {
    return "供应商 / 协议 / 统计类指令必须恰好 8 项（当前 " + supplierCommands.length + " 项）";
  }
  if (supplierCommands.some((item) => !String(item || "").trim())) {
    return "供应商 / 协议 / 统计类指令的 8 项都不能为空";
  }

  /* ---- 权限与范围 ---- */
  const groups = payload.group_list || [];
  if (!Number.isInteger(payload.batch_max) || payload.batch_max < 1 || payload.batch_max > 10) return "批量出图上限必须是 1~10 的整数";
  const blacklist = payload.user_blacklist || [];
  if (blacklist.some((item) => !String(item).trim() || (String(item).includes("|") && String(item).split("|").some((part) => !part.trim())))) return "群友黑名单格式不正确";
  for (const key of ["menu_sections", "prompts", "gacha_styles"]) {
    const entries = payload[key];
    if (!Array.isArray(entries) || entries.length > (key === "menu_sections" ? 20 : 200)) return "菜单或素材条目数量超出限制";
    const seenNames = new Set();
    for (const entry of entries) {
      const entryName = String(entry.name || "").trim();
      if (!entryName || seenNames.has(entryName)) return "菜单和素材名称必须非空且不重复：" + entryName;
      seenNames.add(entryName);
      if (key === "menu_sections" && (!Array.isArray(entry.items) || entry.items.length > 40)) return "每组菜单最多 40 条指令";
      if (key !== "menu_sections" && !String(entry.text || "").trim()) return "请填写「" + entryName + "」的提示词正文";
    }
  }
  for (const item of Object.values(payload.bot_overrides || {})) {
    if (item.protocol && !PROTOCOL_KEYS.includes(item.protocol)) return "机器人独立配置中的协议无效";
    if (item.size && !looksLikeSize(item.size)) return "机器人独立配置中的尺寸无效";
  }

  /* ---- 触发 / 冷却 / 重试 / 代理 ---- */
  if (["at", "command"].indexOf(String(payload.trigger_mode || "")) === -1) {
    return "触发方式必须是「需要 @机器人」或「不需要 @（仅群聊）」之一";
  }
  if (!Number.isFinite(payload.cooldown) || payload.cooldown < 0) return "同用户冷却必须是不小于 0 的数字（0 表示不限制）";
  if (!Number.isFinite(payload.retry_times) || payload.retry_times < 0 || payload.retry_times > 3) {
    return "失败自动重试次数必须在 0 ~ 3 之间";
  }
  const proxy = String(payload.proxy || "").trim();
  if (proxy && !looksLikeUrl(proxy, true)) {
    return "HTTP 代理必须以 http://、https:// 或 socks5:// 开头（留空表示不使用代理）";
  }

  return "";
}

async function save() {
  const button = $("save-btn");
  if (state.saving) return;

  let payload;
  try {
    payload = collectPayload();
  } catch (error) {
    toast("收集配置失败：" + errText(error), "err");
    return;
  }

  const problem = validatePayload(payload);
  if (problem) {
    showError(problem);
    toast(problem, "err");
    return;
  }
  clearError();

  state.saving = true;
  setBusy(button, true, "保存中…");
  try {
    await apiPost("config", payload);
    markClean();
    showOkBanner("✅ 已保存，重载插件后全部生效");
    toast("已保存，重载插件后全部生效", "ok");
    await loadAll({ keepBanner: true });
  } catch (error) {
    const message = "保存失败：" + errText(error);
    showError(message);
    toast(message, "err");
  } finally {
    state.saving = false;
    setBusy(button, false);
  }
}

/* ------------------------------------------------------------ 恢复默认 */

/** 一键把所有表单内容恢复为插件内置默认值（只改内存，保存后才写入）。 */
function applyAllDefaults() {
  const cfg = state.config || {};

  /* 供应商与协议 */
  state.protocolModels = PROTOCOL_KEYS.reduce((result, key) => {
    result[key] = { model: DEFAULT_PROTOCOL_MODELS[key].model, edit_model: DEFAULT_PROTOCOL_MODELS[key].edit_model || "" };
    return result;
  }, {});
  state.suppliers = [{
    name: "默认中转站",
    base_url: DEFAULT_BASE_URLS.openai,
    api_key: "",
    api_key_set: false,
    api_key_masked: "",
  }];
  cfg.active_supplier = "默认中转站";
  state.activeProtocol = "openai";
  cfg.active_protocol = "openai";
  state.modelCache = Object.create(null);

  /* 模型与尺寸 */
  cfg.timeout = 600;
  cfg.max_input_images = 6;
  cfg.max_concurrent = 2;
  cfg.reply_reference_image = true;
  cfg.image_sizes = SIZE_PRESETS.filter((size) => size !== "auto").slice(0, 4);
  cfg.active_size = cfg.image_sizes[0] || "1024x1024";
  if (cfg.image_sizes.indexOf("auto") === -1) cfg.image_sizes.unshift("auto");

  /* 权限与范围 */
  cfg.masters = [];
  cfg.masters_use_astrbot_admin = true;
  cfg.enabled_platforms = PLATFORM_PRESETS.map((item) => item.id);
  cfg.enabled_bot_ids = [];
  cfg.private_enabled = true;
  cfg.group_mode = "all";
  cfg.group_list = [];
  cfg.trigger_mode = "at";
  cfg.cooldown = 0;
  cfg.retry_times = 1;
  cfg.proxy = "";
  cfg.batch_max = 4;
  cfg.size_auto_detect = true;
  cfg.strict_trigger = true;
  cfg.group_require_enable = true;
  cfg.gacha_enabled = true;
  cfg.rank_enabled = true;
  cfg.flavor_enabled = true;
  cfg.translate_enabled = false;
  cfg.img2prompt_enabled = false;
  cfg.user_blacklist = [];
  cfg.bot_overrides = {};
  cfg.menu_sections = [
    { name: "绘画与编辑", items: ["绘画", "图片编辑", "重画", "抽卡", "预设列表", "图片转提示词"] },
    { name: "模型与接口", items: ["模型列表", "切换模型", "协议列表", "切换协议", "供应商列表", "切换供应商"] },
    { name: "尺寸设置", items: ["尺寸列表", "切换尺寸", "添加尺寸", "删除尺寸"] },
    { name: "群与权限", items: ["群列表", "群开关", "添加主人", "删除主人"] },
    { name: "娱乐与统计", items: ["排行", "运行统计", "我的ID"] },
    { name: "插件维护", items: ["菜单列表", "设置菜单", "删除菜单", "添加预设", "删除预设", "重载配置", "重置设置"] },
  ];

  /* 指令与文案 */
  cfg.draw_commands = DEFAULT_COMMANDS.draw.slice();
  cfg.edit_commands = DEFAULT_COMMANDS.edit.slice();
  cfg.menu_commands = DEFAULT_COMMANDS.menu.slice();
  cfg.master_commands = DEFAULT_COMMANDS.master.slice();
  cfg.supplier_commands = DEFAULT_SUPPLIER_COMMANDS.slice();
  ["prompts", "gacha_styles", "flavor_lines", "menu_sections", "translate_prompt_text", "translate_model", "translate_supplier", "img2prompt_model", "start_prompt", "done_prompt", "menu_text", "master_commands", "image_sizes"].forEach((key) => {
    if (Object.prototype.hasOwnProperty.call(cfg.defaults || {}, key)) cfg[key] = cloneDefault(key);
  });
  cfg.group_mode = "whitelist";

  markDirty();
  renderAll();
  paintSummary();
  toast("已恢复为插件内置默认值，请检查后点「💾 保存配置」写入", "info");
}

/* ------------------------------------------------------------ 加载与初始化 */

/** 渲染全部区块（顺序与页面结构一致）。 */
function cloneDefault(key) {
  const value = (state.config && state.config.defaults || {})[key];
  return JSON.parse(JSON.stringify(value === undefined ? [] : value));
}

function renderSizePreview() {
  const body = $("models-body");
  if (!body) return;
  const previous = $("size-support-preview");
  if (previous) previous.remove();
  const cfg = state.config || {};
  const allowed = (cfg.size_list_for || {})[state.activeProtocol] || cfg.image_sizes || [];
  body.appendChild(h("div", { class: "panel-note", id: "size-support-preview" }, [
    h("strong", { text: protocolShortLabelOf(state.activeProtocol) + " · 可用尺寸" }),
    h("div", { class: "size-preview" }, allowed.map((size) => pill(size, "plain"))),
    hint("按当前协议的尺寸规则筛选；最终支持范围以所选模型和服务商为准。"),
  ]));
}

function editField(label, value, onChange, multiline) {
  return h("label", { class: "field" }, [
    h("span", { class: "field-label", text: label }),
    h(multiline ? "textarea" : "input", {
      class: multiline ? "textarea" : "input",
      value: String(value ?? ""), rows: multiline ? "3" : null,
      oninput: (event) => { onChange(event.currentTarget.value); markDirty(); },
    }),
  ]);
}

function createCollectionEditor(key, title, limit) {
  const cfg = state.config;
  if (!Array.isArray(cfg[key])) cfg[key] = cloneDefault(key);
  const panel = h("details", { class: "collection-panel", id: key + "-editor", open: key === "menu_sections" });
  const summary = h("summary");
  const list = h("div", { class: "editor-list" });
  function paint() {
    clear(list);
    summary.textContent = title + " · " + cfg[key].length + " 条";
    cfg[key].forEach((entry, index) => {
      const card = h("div", { class: "editor-item" });
      const actions = h("div", { class: "item-actions" });
      function move(offset) {
        const target = index + offset;
        if (target < 0 || target >= cfg[key].length) return;
        const moved = cfg[key].splice(index, 1)[0];
        cfg[key].splice(target, 0, moved);
        markDirty(); paint();
      }
      actions.appendChild(h("button", { type: "button", class: "btn tiny", text: "上移", disabled: index === 0, onclick: () => move(-1) }));
      actions.appendChild(h("button", { type: "button", class: "btn tiny", text: "下移", disabled: index === cfg[key].length - 1, onclick: () => move(1) }));
      actions.appendChild(h("button", { type: "button", class: "btn tiny", text: "删除", onclick: () => { cfg[key].splice(index, 1); markDirty(); paint(); } }));
      card.appendChild(h("div", { class: "editor-item-head" }, [h("strong", { text: String(index + 1) }), actions]));
      card.appendChild(editField(key === "menu_sections" ? "分组名称" : "名称", entry.name, (value) => { entry.name = value; }));
      if (key === "menu_sections") {
        card.appendChild(editField("指令（每行一条，也可用 | 分隔）", (entry.items || []).join("\n"), (value) => {
          entry.items = value.split(/[\n|、]/).map((item) => item.trim()).filter(Boolean);
        }, true));
      } else {
        card.appendChild(editField("提示词正文", entry.text, (value) => { entry.text = value; }, true));
        if (key === "prompts") {
          card.appendChild(editField("标签（逗号分隔）", (entry.tags || []).join(", "), (value) => { entry.tags = value.split(/[,，]/).map((item) => item.trim()).filter(Boolean); }));
        } else {
          card.appendChild(h("label", { class: "field" }, [h("span", { class: "field-label", text: "稀有度" }), h("select", {
            class: "select", onchange: (event) => { entry.rarity = event.currentTarget.value; markDirty(); },
          }, ["common", "rare", "epic"].map((rarity, rarityIndex) => h("option", { value: rarity, selected: (entry.rarity || "common") === rarity, text: ["普通", "稀有", "史诗"][rarityIndex] })))]));
        }
      }
      list.appendChild(card);
    });
    if (!cfg[key].length) list.appendChild(hint("暂无条目，可新增或恢复出厂内容。"));
  }
  panel.appendChild(summary);
  panel.appendChild(list);
  panel.appendChild(h("div", { class: "row" }, [
    h("button", { type: "button", class: "btn", text: "新增" + title, onclick: () => {
      if (cfg[key].length >= limit) { toast("最多 " + limit + " 条", "err"); return; }
      cfg[key].push(key === "menu_sections" ? { name: "", items: [] } : { name: "", text: "", ...(key === "prompts" ? { tags: [] } : { rarity: "common" }) });
      panel.open = true; markDirty(); paint();
    } }),
    h("button", { type: "button", class: "btn ghost", text: "恢复出厂" + title, onclick: () => { cfg[key] = cloneDefault(key); markDirty(); paint(); } }),
  ]));
  paint();
  return panel;
}

function renderBotOverrides() {
  const cfg = state.config;
  const body = $("access-body");
  if (!body) return;
  const previous = $("bot-overrides-editor");
  if (previous) previous.remove();
  const panel = h("div", { class: "card flat", id: "bot-overrides-editor" });
  const list = h("div", { class: "kv-list" });
  if (!cfg.bot_overrides || typeof cfg.bot_overrides !== "object") cfg.bot_overrides = {};
  function paint() {
    clear(list);
    Object.entries(cfg.bot_overrides).forEach(([instanceId, entry]) => {
      const row = h("div", { class: "editor-item" }, [h("strong", { text: instanceId })]);
      function selectField(label, field, choices) {
        const value = entry[field] || "";
        const options = [{ value: "", text: "沿用全局" }, ...choices];
        if (value && !options.some((option) => option.value === value)) options.push({ value, text: value });
        return h("label", { class: "field" }, [h("span", { class: "field-label", text: label }), h("select", {
          class: "select", onchange: (event) => { entry[field] = event.currentTarget.value; markDirty(); },
        }, options.map((option) => h("option", { ...option, selected: option.value === value }))) ]);
      }
      row.appendChild(h("div", { class: "grid" }, [
        selectField("供应商", "supplier", state.suppliers.map((supplier) => ({ value: supplier.name, text: supplier.name }))),
        selectField("协议", "protocol", PROTOCOL_KEYS.map((protocol) => ({ value: protocol, text: protocolShortLabelOf(protocol) }))),
        editField("模型（留空沿用全局）", entry.model, (value) => { entry.model = value; }),
        selectField("尺寸", "size", trimList(cfg.image_sizes).map((size) => ({ value: size, text: size }))),
      ]));
      row.appendChild(h("button", { type: "button", class: "btn tiny", text: "清除该实例配置", onclick: () => { delete cfg.bot_overrides[instanceId]; markDirty(); paint(); } }));
      list.appendChild(row);
    });
  }
  const candidates = h("datalist", { id: "override-instance-options" }, state.platformInstances.map((instance) => h("option", { value: instance.id, text: instance.name || instance.id })));
  const input = h("input", { class: "input", list: "override-instance-options", placeholder: "选择或输入机器人实例 ID", "aria-label": "独立配置的机器人实例 ID" });
  panel.appendChild(h("div", { class: "card-head" }, [h("h3", { text: "机器人实例独立配置" })]));
  panel.appendChild(h("div", { class: "card-body" }, [
    hint("未填写的字段沿用全局。可从已获取的实例中选择，也可手动填写实例 ID。"),
    h("div", { class: "row" }, [input, h("button", { type: "button", class: "btn", text: "添加实例配置", onclick: () => {
      const instanceId = input.value.trim();
      if (!instanceId) { toast("请先选择或输入实例 ID", "err"); return; }
      if (!Object.prototype.hasOwnProperty.call(cfg.bot_overrides, instanceId)) Object.defineProperty(cfg.bot_overrides, instanceId, { value: {}, writable: true, enumerable: true, configurable: true });
      input.value = ""; markDirty(); paint();
    } })]), candidates, list,
  ]));
  body.appendChild(panel);
  paint();
}

function createSmartModelChooser(options) {
  const opts = options || {};
  const cfg = state.config || {};
  let cacheKey = "";
  let requestNumber = 0;
  let candidates = [];
  const source = () => ({
    supplier: String((opts.purpose === "text" && cfg.translate_supplier) || activeSupplierName()).trim(),
    protocol: String(state.activeProtocol || "openai"),
  });
  const input = h("input", {
    id: opts.id,
    class: "input",
    type: "text",
    "aria-label": opts.label,
    "aria-describedby": opts.id + "-hint",
    list: opts.id + "-options",
    value: String(cfg[opts.configKey] || ""),
    placeholder: "填写模型名或从供应商返回的候选中选择",
    oninput: (event) => {
      cfg[opts.configKey] = event.currentTarget.value;
      markDirty();
    },
  });
  const result = h("div", { class: "result" });
  const sourceLabel = h("div", { class: "hint" });
  const list = h("div", { class: "model-list smart-model-list", role: "group", "aria-label": opts.label + "候选" });
  const datalist = h("datalist", { id: opts.id + "-options" });
  const search = h("input", {
    id: opts.id + "-search", class: "input", type: "search", hidden: true,
    placeholder: "搜索完整模型列表…", "aria-label": "搜索" + opts.label + "候选",
    oninput: () => paint(),
  });
  const paint = () => {
    clear(datalist);
    clear(list);
    const needle = search.value.trim().toLowerCase();
    candidates.forEach((model, index) => {
      datalist.appendChild(h("option", { value: model }));
      if (needle && !model.toLowerCase().includes(needle)) return;
      list.appendChild(h("button", {
        type: "button",
        class: "chip model-item",
        title: "选择第 " + (index + 1) + " 个供应商返回的模型候选",
        onclick: () => {
          if (JSON.stringify(source()) !== cacheKey) {
            refresh();
            return;
          }
          input.value = model;
          cfg[opts.configKey] = model;
          markDirty();
          setResult(result, "已选择模型：" + model + "（保存后生效）", "ok");
        },
      }, [h("span", { class: "model-index", text: String(index + 1) }), h("span", { text: model })]));
    });
    if (candidates.length && !list.childNodes.length) list.appendChild(hint("没有匹配的模型"));
    search.hidden = candidates.length === 0;
    list.hidden = candidates.length === 0;
  };
  const fetchButton = h("button", {
    type: "button",
    id: opts.id + "-fetch",
    class: "btn tiny",
    text: "📋 获取完整模型列表",
    "aria-label": "为" + opts.label + "获取完整模型列表",
    onclick: async (event) => {
      const button = event.currentTarget;
      refresh();
      const requestedSource = source();
      if (!requestedSource.supplier) {
        setResult(result, "请先配置并保存供应商，再获取模型列表。", "err");
        return;
      }
      const requestedKey = cacheKey;
      const currentRequest = ++requestNumber;
      setBusy(button, true, "获取中…");
      setResult(result, "正在获取供应商返回的完整模型列表…", "info");
      try {
        const modelResult = await apiGet("models", {
          supplier: requestedSource.supplier,
          protocol: requestedSource.protocol,
          include_all: 1,
          refresh: 1,
          purpose: opts.purpose,
        });
        if (currentRequest !== requestNumber || JSON.stringify(source()) !== requestedKey || !input.isConnected) return;
        if (!modelResult.ok) {
          const fallbackModels = state.modelCache[modelCacheKey(requestedSource.supplier, requestedSource.protocol)] || [];
          candidates = fallbackModels;
          paint();
          setResult(result, fallbackModels.length ? "⚠️ 在线获取失败，已回退到缓存 " + fallbackModels.length + " 个模型" : "❌ 获取模型失败：" + modelResult.message, fallbackModels.length ? "warn" : "err");
          return;
        }
        const models = Array.from(new Set(modelResult.models));
        rememberModels(requestedSource.supplier, requestedSource.protocol, modelResult, "在线接口");
        state.smartModelCache[opts.purpose + requestedKey] = models;
        candidates = models;
        paint();
        setResult(result, "✅ 获取到 " + models.length + " 个候选模型；用途需供应商支持。", "ok");
      } catch (error) {
        if (currentRequest === requestNumber && JSON.stringify(source()) === requestedKey) {
          setResult(result, "❌ 获取失败：" + errText(error), "err");
        }
      } finally {
        if (currentRequest === requestNumber) setBusy(button, false);
      }
    },
  });
  const panel = h("div", { class: "smart-model-picker" }, [
    sourceLabel,
    h("div", { class: "row tight" }, [input, fetchButton]),
    datalist,
    search,
    list,
    h("div", { class: "hint", id: opts.id + "-hint", text: "候选为供应商返回的完整列表，包含图片、文本等模型名称；不据名称判断能力，所选用途需供应商支持。修改地址或密钥后请先保存再获取。" }),
    result,
  ]);
  const refresh = () => {
    const currentSource = source();
    const nextKey = JSON.stringify(currentSource);
    sourceLabel.textContent = "供应商：" + (currentSource.supplier || "未选择") + " · 协议：" + protocolShortLabelOf(currentSource.protocol);
    if (nextKey === cacheKey) return;
    cacheKey = nextKey;
    requestNumber += 1;
    setBusy(fetchButton, false);
    candidates = state.smartModelCache[opts.purpose + cacheKey]
      || state.modelCache[modelCacheKey(currentSource.supplier, currentSource.protocol)]
      || [];
    search.value = "";
    setResult(result, "", "info");
    paint();
  };
  state.smartModelRefreshers.push(refresh);
  refresh();
  return panel;
}

function refreshSmartModelSources() {
  const cfg = state.config || {};
  const select = $("translate-supplier");
  if (select) {
    const selected = String(cfg.translate_supplier || "");
    const names = Array.from(new Set(trimList(state.suppliers.map((item) => item.name))));
    const signature = JSON.stringify([selected, names]);
    if (select.dataset.sources !== signature) {
      clear(select);
      select.appendChild(h("option", { value: "", text: "沿用当前绘画供应商" }));
      if (selected && !names.includes(selected)) names.push(selected);
      names.forEach((name) => select.appendChild(h("option", { value: name, text: name })));
      select.value = selected;
      select.dataset.sources = signature;
    }
  }
  state.smartModelRefreshers.forEach((refresh) => refresh());
}

function renderSmart() {
  const body = $("smart-body");
  if (!body) return;
  clear(body);
  state.smartModelRefreshers = [];
  const cfg = state.config || {};
  const toggle = (id, title, desc, value) => h("label", { class: "switch-row" }, [
    h("span", { class: "switch-row-text" }, [
      h("span", { class: "switch-row-title", text: title }),
      h("span", { class: "switch-row-desc", text: desc }),
    ]),
    h("input", { id, type: "checkbox", checked: Boolean(value), onchange: (event) => { cfg[id.replace(/-/g, "_")] = event.currentTarget.checked; markDirty(); } }),
  ]);
  const translateSupplierSelect = h("select", { id: "translate-supplier", class: "select", onchange: (event) => { cfg.translate_supplier = event.currentTarget.value; refreshSmartModelSources(); markDirty(); } }, [
    h("option", { value: "", text: "沿用当前绘画供应商", selected: !cfg.translate_supplier }),
    ...toArray(state.suppliers).map((item) => h("option", { value: item.name, text: item.name, selected: item.name === cfg.translate_supplier })),
  ]);
  const smartGrid = h("div", { class: "smart-grid" }, [
    h("div", { class: "sub-block" }, [
      h("h3", { class: "sub-title", text: "🛡 触发与翻译" }),
      toggle("strict-trigger", "误触发保护", "过滤「画画看」等没有实际内容的闲聊。", cfg.strict_trigger !== false),
      toggle("translate-enabled", "中文提示词翻译", "先翻译成英文再请求图片接口，需要翻译模型。", cfg.translate_enabled),
      h("div", { class: "field" }, [h("label", { class: "field-label", for: "translate-model", text: "翻译模型" }), createSmartModelChooser({ id: "translate-model", label: "翻译模型", configKey: "translate_model", purpose: "text" })]),
      h("label", { class: "field" }, [h("span", { class: "field-label", text: "翻译供应商" }), translateSupplierSelect]),
      h("label", { class: "field" }, [h("span", { class: "field-label", text: "翻译系统提示词" }), h("textarea", { id: "translate-prompt-text", class: "textarea", value: String(cfg.translate_prompt_text ?? ""), oninput: (event) => { cfg.translate_prompt_text = event.currentTarget.value; markDirty(); } })]),
    ]),
    h("div", { class: "sub-block" }, [
      h("h3", { class: "sub-title", text: "🎲 玩法与视觉" }),
      toggle("gacha-enabled", "每日抽卡", "每天一次随机画风与主题。", cfg.gacha_enabled !== false),
      toggle("rank-enabled", "绘画排行", "记录本周与累计绘画数量。", cfg.rank_enabled !== false),
      toggle("flavor-enabled", "出图文案", "完成后随机发送一句文案。", cfg.flavor_enabled !== false),
      toggle("img2prompt-enabled", "图片转提示词", "用视觉模型从图片反推提示词。", cfg.img2prompt_enabled),
      h("div", { class: "field" }, [h("label", { class: "field-label", for: "img2prompt-model", text: "视觉模型" }), createSmartModelChooser({ id: "img2prompt-model", label: "视觉模型", configKey: "img2prompt_model", purpose: "vision" })]),
    ]),
  ]);
  body.appendChild(smartGrid);
}

function renderAll() {
  renderOverview();
  renderSuppliers();
  renderModels();
  renderSizePreview();
  renderCommands();
  renderPrompts();
  renderAccess();
  renderSmart();
  paintSummary();
  updateCompletion();
}

/**
 * 从供应商数据里挑选一个可用的当前供应商名称（避免出现「当前供应商不在列表」）。
 */
function pickActiveSupplier(cfg, suppliers) {
  const wanted = String(cfg.active_supplier || cfg.active_provider || "");
  if (wanted && suppliers.some((item) => item.name === wanted)) return wanted;
  return suppliers.length ? suppliers[0].name : "";
}

async function loadAll(options) {
  const opts = options || {};
  const loadState = $("load-state");
  if (loadState) loadState.textContent = "加载中…";

  clearError();

  let config = null;
  let supplierDirectory = null;
  let status = null;
  let stats = null;
  const failures = [];

  const results = await Promise.allSettled([apiGet("config"), apiGet("suppliers"), apiGet("status"), apiGet("stats")]);
  if (results[0].status === "fulfilled") config = results[0].value;
  else failures.push("配置读取失败：" + errText(results[0].reason));
  if (results[1].status === "fulfilled") supplierDirectory = results[1].value;
  else failures.push("供应商目录读取失败：" + errText(results[1].reason));
  if (results[2].status === "fulfilled") status = results[2].value;
  else failures.push("状态读取失败：" + errText(results[2].reason));
  if (results[3].status === "fulfilled") stats = results[3].value;
  else failures.push("统计读取失败：" + errText(results[3].reason));

  if (!config) {
    const message = failures.join("；") || "无法读取插件配置";
    showError(message + "。请确认插件已启用、AstrBot 版本 >= 4.19，并重试。");
    if (loadState) loadState.textContent = "";
    renderFallback(message);
    return false;
  }

  state.config = config;
  for (const key of ["start_prompt", "done_prompt", "menu_text"]) {
    if (typeof (config.defaults || {})[key] === "string") DEFAULT_PROMPTS[key] = config.defaults[key];
  }
  state.status = status || {};
  state.stats = stats || {};

  /* 向后兼容：新后端返回 suppliers，老后端只有 providers。 */
  const configSuppliers = supplierListFromValue(config.suppliers).filter(Boolean);
  const configProviders = supplierListFromValue(config.providers).filter(Boolean);
  const directorySuppliers = supplierListFromValue(supplierDirectory && (
    supplierDirectory.suppliers || supplierDirectory.providers || supplierDirectory.channels || supplierDirectory
  )).filter(Boolean);
  const rawSuppliers = configSuppliers.length ? configSuppliers : (configProviders.length ? configProviders : directorySuppliers);
  const normalizedSuppliers = rawSuppliers.map(normalizeSupplier).filter((item) => item.name);
  const directoryByName = new Map(directorySuppliers.map((item) => normalizeSupplier(item)).filter((item) => item.name).map((item) => [item.name, item]));
  state.suppliers = normalizedSuppliers.map((item) => {
    const extra = directoryByName.get(item.name);
    return extra ? { ...extra, ...item, protocols: item.protocols || extra.protocols } : item;
  });
  if (!state.suppliers.length && directorySuppliers.length) {
    state.suppliers = directorySuppliers.map(normalizeSupplier).filter((item) => item.name);
  }

  /* 协议顺序与标签（后端可能只返回部分协议，缺失时用内置常量补齐）。 */
  const protocolOptions = toArray(config.protocols);
  const ordered = [];
  protocolOptions.forEach((item) => {
    const key = typeof item === "string" ? item : String((item && (item.key || item.value)) || "");
    if (key && PROTOCOL_KEYS.indexOf(key) !== -1 && ordered.indexOf(key) === -1) ordered.push(key);
    if (item && item.label && key) state.protocolLabels[key] = String(item.label);
  });
  PROTOCOL_KEYS.forEach((key) => {
    if (ordered.indexOf(key) === -1) ordered.push(key);
  });
  state.protocolOrder = ordered;

  /* 协议模型：优先读 protocol_models，缺失时回退到旧 providers 的 model 字段。 */
  const directoryModels = supplierDirectory && supplierDirectory.protocol_models;
  const rawModels = config.protocol_models && typeof config.protocol_models === "object"
    ? config.protocol_models
    : (directoryModels && typeof directoryModels === "object" ? directoryModels : {});
  PROTOCOL_KEYS.forEach((key) => {
    const entry = rawModels[key] && typeof rawModels[key] === "object" ? rawModels[key] : {};
    const fallback = DEFAULT_PROTOCOL_MODELS[key] || { model: "", edit_model: "" };
    state.protocolModels[key] = {
      model: String(entry.model || fallback.model || ""),
      edit_model: String(entry.edit_model || ""),
    };
  });
  /* 老后端降级：没有 protocol_models 时，用旧通道的 model / edit_model 补全对应协议。 */
  const legacyModels = !config.protocol_models || typeof config.protocol_models !== "object";
  if (legacyModels) {
    const legacyList = toArray(config.providers);
    legacyList.forEach((item) => {
      const proto = PROTOCOL_KEYS.indexOf(String(item.protocol || item.__template_key || "")) !== -1
        ? String(item.protocol || item.__template_key)
        : "";
      if (!proto || !item.model) return;
      if (!state.protocolModels[proto].model) state.protocolModels[proto].model = String(item.model);
      if (!state.protocolModels[proto].edit_model && item.edit_model) {
        state.protocolModels[proto].edit_model = String(item.edit_model);
      }
    });
  }

  const rawProtocol = String(config.active_protocol || "");
  state.activeProtocol = PROTOCOL_KEYS.indexOf(rawProtocol) !== -1 ? rawProtocol : "openai";
  config.active_protocol = state.activeProtocol;

  config.active_supplier = pickActiveSupplier(
    { ...config, active_supplier: config.active_supplier || (supplierDirectory && supplierDirectory.active) },
    state.suppliers,
  );
  loadModelCache(
    { ...config, model_cache: config.model_cache || (supplierDirectory && supplierDirectory.model_cache) },
    config.active_supplier,
  );

  state.version = (status && status.version) || FALLBACK_VERSION;
  const versionNode = $("version-badge");
  if (versionNode) versionNode.textContent = "v" + state.version;

  try {
    renderAll();
  } catch (error) {
    showError("渲染界面失败：" + errText(error));
    throw error;
  }

  state.loaded = true;
  markClean();
  if (loadState) loadState.textContent = "";
  if (failures.length) {
    showError(failures.join("；"));
  } else {
    clearError();
  }
  return failures.length === 0;
}

function renderFallback(message) {
  ["models-body", "commands-body", "prompts-body", "access-body", "smart-body"].forEach((id) => {
    const node = $(id);
    if (!node) return;
    clear(node);
    node.appendChild(h("div", { class: "inline-note", text: message }));
  });
  ["suppliers-body", "overview-body"].forEach((id) => {
    const node = $(id);
    if (!node) return;
    clear(node);
    node.appendChild(h("div", { class: "card" }, [
      h("div", { class: "card-body" }, [h("div", { class: "inline-note", text: message })]),
    ]));
  });
}

/* ------------------------------------------------------------ 交互与初始化 */

function legacyUpdatePageVisibility() {
  const sections = Array.from(document.querySelectorAll(".content > .section"));
  if (!sections.length) return;
  state.pageIndex = Math.min(Math.max(state.pageIndex, 0), sections.length - 1);
  sections.forEach((section, index) => {
    const active = index === state.pageIndex;
    section.removeAttribute("hidden");
    section.setAttribute("aria-current", active ? "true" : "false");
    section.classList.toggle("is-current", active);
  });
  document.querySelectorAll("#side-nav .nav-link").forEach((link, index) => {
    const active = index === state.pageIndex;
    link.classList.toggle("active", active);
    link.setAttribute("aria-selected", active ? "true" : "false");
    link.setAttribute("tabindex", active ? "0" : "-1");
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  const indicator = $("page-indicator");
  if (indicator) indicator.textContent = "第 " + (state.pageIndex + 1) + " / " + sections.length + " 页";
  const previous = $("page-prev");
  const next = $("page-next");
  if (previous) previous.disabled = state.pageIndex === 0;
  if (next) next.disabled = state.pageIndex === sections.length - 1;
  const select = $("page-select");
  if (select) select.value = sections[state.pageIndex].id;
}

function legacyBindSectionNavigation() {
  const sections = Array.from(document.querySelectorAll(".content > .section"));
  const links = Array.from(document.querySelectorAll("#side-nav .nav-link"));
  const select = $("page-select");
  if (!sections.length || !links.length) return;
  sections.forEach((section, index) => {
    const link = links[index];
    link.id = "tab-" + section.id;
    section.setAttribute("role", "tabpanel");
    section.setAttribute("aria-labelledby", link.id);
    section.tabIndex = -1;
    select.appendChild(h("option", { value: section.id, text: link.querySelector(".nav-text").textContent }));
    link.addEventListener("click", (event) => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
      event.preventDefault();
      scrollToSection("#" + section.id, { focus: false });
    });
    link.addEventListener("keydown", (event) => {
      let nextIndex = index;
      if (event.key === "ArrowLeft" || event.key === "ArrowUp") nextIndex = (index + links.length - 1) % links.length;
      else if (event.key === "ArrowRight" || event.key === "ArrowDown") nextIndex = (index + 1) % links.length;
      else if (event.key === "Home") nextIndex = 0;
      else if (event.key === "End") nextIndex = links.length - 1;
      else if (event.key !== " ") return;
      event.preventDefault();
      scrollToSection(links[nextIndex].getAttribute("href"), { focus: false, scroll: false });
      links[nextIndex].focus({ preventScroll: true });
      links[nextIndex].scrollIntoView({ block: "nearest", inline: "nearest" });
    });
  });
  $("page-prev").addEventListener("click", () => {
    if (state.pageIndex > 0) scrollToSection("#" + sections[state.pageIndex - 1].id);
  });
  $("page-next").addEventListener("click", () => {
    if (state.pageIndex < sections.length - 1) scrollToSection("#" + sections[state.pageIndex + 1].id);
  });
  select.addEventListener("change", () => scrollToSection("#" + select.value, { focus: false }));
  const followHash = (initial) => {
    const section = sections.find((item) => "#" + item.id === window.location.hash) || sections[0];
    scrollToSection("#" + section.id, { fromHistory: true, focus: !initial, scroll: !initial });
  };
  window.addEventListener("hashchange", () => followHash(false));
  window.addEventListener("popstate", () => followHash(false));
  const topbar = document.querySelector(".topbar");
  const updateHeaderHeight = () => {
    document.documentElement.style.setProperty("--settings-header-height", topbar.getBoundingClientRect().height + "px");
    $("side-nav").setAttribute("aria-orientation", window.innerWidth <= 1024 ? "horizontal" : "vertical");
  };
  updateHeaderHeight();
  window.addEventListener("resize", updateHeaderHeight);
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(updateHeaderHeight).observe(topbar);
  }
  followHash(true);
}

function updatePageVisibility() {
  const sections = Array.from(document.querySelectorAll(".content > .section"));
  if (!sections.length) return;
  const active = sections.find((section) => section.id === state.activeSectionId) || sections[0];
  state.activeSectionId = active.id;
  sections.forEach((section) => {
    const isCurrent = section === active;
    section.hidden = !isCurrent;
    section.classList.toggle("is-current", isCurrent);
    section.setAttribute("aria-current", isCurrent ? "page" : "false");
    if (isCurrent) section.setAttribute("open", "");
    else section.removeAttribute("open");
  });
  document.querySelectorAll("#top-section-tabs [role=tab]").forEach((tab) => {
    const isCurrent = tab.getAttribute("aria-controls") === active.id;
    tab.classList.toggle("active", isCurrent);
    tab.setAttribute("aria-selected", isCurrent ? "true" : "false");
    tab.tabIndex = isCurrent ? 0 : -1;
  });
}

function bindSectionNavigation() {
  const content = document.querySelector(".content");
  const sections = Array.from(document.querySelectorAll(".content > .section"));
  const legacyNav = $("side-nav");
  if (!content || !sections.length) return;

  const labels = Array.from(document.querySelectorAll("#side-nav .nav-link")).map((link) => {
    const label = link.querySelector(".nav-text");
    return label ? label.textContent.trim() : link.textContent.trim();
  });
  sections.forEach((section) => {
    section.querySelectorAll(".section-no").forEach((node) => node.remove());
    section.setAttribute("role", "tabpanel");
    section.tabIndex = -1;
  });
  document.querySelectorAll("#side-nav .nav-index").forEach((node) => node.remove());
  if (legacyNav) {
    legacyNav.hidden = true;
    legacyNav.setAttribute("aria-hidden", "true");
  }
  const legacyPageNav = $("page-nav");
  if (legacyPageNav) {
    legacyPageNav.hidden = true;
    legacyPageNav.setAttribute("aria-hidden", "true");
  }

  if (!$("section-tabs-style")) {
    document.head.appendChild(h("style", {
      id: "section-tabs-style",
      text: `
        .layout { display: block; }
        #side-nav, #page-nav { display: none !important; }
        .content > .section[hidden] { display: none !important; }
        .content > .section:not([hidden]) { display: block !important; }
        .section-summary { display: none !important; }
        .section-content { padding-top: 20px; }
        .top-section-tabs { display: flex; gap: 8px; margin: 0 0 18px; padding: 6px; overflow-x: auto; border: 1px solid var(--border); border-radius: 16px; background: color-mix(in srgb, var(--panel) 94%, transparent); box-shadow: var(--shadow-1); scrollbar-width: thin; }
        .section-tab { flex: 0 0 auto; min-height: 40px; padding: 9px 14px; border: 1px solid transparent; border-radius: 11px; color: var(--muted); background: transparent; font: inherit; font-size: 13px; font-weight: 750; white-space: nowrap; cursor: pointer; transition: color var(--dur-base) var(--ease), background var(--dur-base) var(--ease), border-color var(--dur-base) var(--ease), transform var(--dur-base) var(--ease); }
        .section-tab:hover { color: var(--text-strong); background: var(--accent-softer); transform: translateY(-1px); }
        .section-tab.active { color: var(--accent-strong); border-color: color-mix(in srgb, var(--accent) 35%, var(--border)); background: var(--accent-weak); box-shadow: 0 5px 14px color-mix(in srgb, var(--accent) 13%, transparent); }
        .section-tab:focus-visible { outline: none; box-shadow: var(--ring); }
        @media (max-width: 640px) { .top-section-tabs { margin-inline: -2px; border-radius: 13px; } .section-tab { min-height: 38px; padding-inline: 11px; font-size: 12px; } .section-content { padding-top: 14px; } }
      `,
    }));
  }

  let tabBar = $("top-section-tabs");
  if (!tabBar) {
    tabBar = h("nav", {
      id: "top-section-tabs",
      class: "top-section-tabs",
      role: "tablist",
      "aria-label": "配置分区",
    });
    content.parentNode.insertBefore(tabBar, content);
  }
  clear(tabBar);
  sections.forEach((section, index) => {
    const tab = h("button", {
      id: "tab-" + section.id,
      class: "section-tab",
      type: "button",
      role: "tab",
      "aria-controls": section.id,
      "aria-selected": "false",
      tabIndex: -1,
      text: labels[index] || section.querySelector(".summary-main strong")?.textContent || section.id,
      onclick: () => scrollToSection("#" + section.id),
      onkeydown: (event) => {
        const currentIndex = sections.indexOf(section);
        let nextIndex = currentIndex;
        if (event.key === "ArrowLeft" || event.key === "ArrowUp") nextIndex = (currentIndex + sections.length - 1) % sections.length;
        else if (event.key === "ArrowRight" || event.key === "ArrowDown") nextIndex = (currentIndex + 1) % sections.length;
        else if (event.key === "Home") nextIndex = 0;
        else if (event.key === "End") nextIndex = sections.length - 1;
        else if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        const target = sections[nextIndex];
        scrollToSection("#" + target.id, { focus: false });
        const targetTab = $("tab-" + target.id);
        if (targetTab) targetTab.focus({ preventScroll: true });
      },
    });
    section.setAttribute("aria-labelledby", tab.id);
    tabBar.appendChild(tab);
  });

  document.querySelectorAll('a[href^="#section-"]').forEach((link) => {
    if (link.dataset.sectionTabBound === "true") return;
    link.dataset.sectionTabBound = "true";
    link.addEventListener("click", (event) => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
      const target = link.getAttribute("href");
      if (!target || !sections.some((section) => "#" + section.id === target)) return;
      event.preventDefault();
      scrollToSection(target);
    });
  });

  const followHash = () => {
    const requestedId = window.location.hash.replace(/^#/, "");
    const section = sections.find((item) => item.id === requestedId) || sections[0];
    state.activeSectionId = section.id;
    updatePageVisibility();
  };
  window.addEventListener("hashchange", followHash);
  window.addEventListener("popstate", followHash);
  const topbar = document.querySelector(".topbar");
  const updateHeaderHeight = () => {
    if (topbar) document.documentElement.style.setProperty("--settings-header-height", topbar.getBoundingClientRect().height + "px");
  };
  updateHeaderHeight();
  window.addEventListener("resize", updateHeaderHeight);
  if (typeof ResizeObserver === "function" && topbar) new ResizeObserver(updateHeaderHeight).observe(topbar);
  followHash();
}

function bindShell() {
  const actions = document.querySelector(".topbar-actions");
  if (actions && !$("update-check-btn")) {
    const updateButton = h("button", { id: "update-check-btn", type: "button", class: "btn ghost", text: "检查更新", title: "检查插件最新版本" });
    updateButton.addEventListener("click", async () => {
      setBusy(updateButton, true, "检查中…");
      try {
        const result = await apiGet("update-check");
        const message = result.message || "检查完成";
        if (result.update_available && result.download_url) {
          toast(message, "ok");
          setResult($("ok-banner"), message + "：" + result.download_url, "ok");
        } else toast(message, result.ok === false ? "err" : "info");
      } catch (error) { toast("检查更新失败：" + errText(error), "err"); }
      finally { setBusy(updateButton, false); }
    });
    actions.insertBefore(updateButton, actions.firstChild);
  }
  const saveBtn = $("save-btn");
  if (saveBtn) saveBtn.addEventListener("click", () => save());

  const retryBtn = $("retry-btn");
  if (retryBtn) {
    retryBtn.addEventListener("click", async () => {
      setBusy(retryBtn, true, "加载中…");
      try {
        const loaded = await loadAll();
        if (loaded) toast("已重新加载，当前页面与服务端配置一致", "ok");
      } catch (error) {
        showError("加载失败：" + errText(error));
        toast("加载失败：" + errText(error), "err");
      } finally {
        setBusy(retryBtn, false);
      }
    });
  }

  document.querySelectorAll("[data-copy]").forEach((node) => {
    node.tabIndex = 0;
    node.setAttribute("role", "button");
    node.setAttribute("aria-label", "复制示例：" + node.textContent);
    node.title = "点击或按 Enter / 空格复制";
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        if (!event.repeat) node.click();
      }
    });
    node.addEventListener("click", async () => {
      if (node.dataset.copying === "true") return;
      node.dataset.copying = "true";
      const text = node.textContent || "";
      const previousFocus = document.activeElement;
      let copied = false;
      try {
        if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
          await navigator.clipboard.writeText(text);
          copied = true;
        }
      } catch (error) {
        copied = false;
      }
      if (!copied) {
        let textarea = null;
        try {
          textarea = h("textarea", { class: "clipboard-copy-buffer", value: text, readOnly: true, tabindex: "-1" });
          document.body.appendChild(textarea);
          textarea.focus({ preventScroll: true });
          textarea.select();
          textarea.setSelectionRange(0, text.length);
          copied = typeof document.execCommand === "function" && document.execCommand("copy") === true;
        } catch (error) {
          copied = false;
        } finally {
          if (textarea) textarea.remove();
          if (previousFocus && previousFocus.isConnected) previousFocus.focus({ preventScroll: true });
        }
      }
      delete node.dataset.copying;
      if (copied) {
        toast("已复制：" + text, "ok");
        return;
      }
      copyReturnFocus = previousFocus;
      $("manual-copy-text").value = text;
      $("manual-copy-dialog").hidden = false;
      $("manual-copy-text").focus();
      $("manual-copy-text").select();
    });
  });

  let copyReturnFocus = null;
  const copyDialog = $("manual-copy-dialog");
  const closeCopyDialog = () => {
    copyDialog.hidden = true;
    if (copyReturnFocus && copyReturnFocus.isConnected && !copyReturnFocus.closest("[hidden]")) {
      copyReturnFocus.focus({ preventScroll: true });
    }
  };
  $("manual-copy-close").addEventListener("click", closeCopyDialog);
  $("manual-copy-select").addEventListener("click", () => {
    $("manual-copy-text").focus();
    $("manual-copy-text").select();
  });
  copyDialog.addEventListener("click", (event) => {
    if (event.target === copyDialog) closeCopyDialog();
  });
  copyDialog.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeCopyDialog();
    } else if (event.key === "Tab") {
      const first = $("manual-copy-text");
      const last = $("manual-copy-close");
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  });

  window.addEventListener("beforeunload", (event) => {
    if (!state.dirty) return undefined;
    event.preventDefault();
    event.returnValue = "";
    return "";
  });
}

async function main() {
  bindShell();
  bindThemeSwitcher();
  bindSectionNavigation();
  if (!document.title || document.title.indexOf(PLUGIN_TITLE) === -1) {
    document.title = PLUGIN_TITLE + " · 插件设置";
  }

  if (!bridge) {
    showError("未检测到 AstrBot Page bridge：请从 AstrBot 插件详情页打开本页面（而不是直接访问 HTML 文件）。");
    renderFallback("bridge 未注入，无法读取配置。");
    return;
  }

  try {
    const context = await bridge.ready();
    const username = context && context.username ? String(context.username) : "";
    const loadState = $("load-state");
    if (loadState && username) loadState.textContent = "已连接：" + username;
    if (bridge.onContext) {
      bridge.onContext(() => {
        let stored = "light";
        try { stored = window.localStorage.getItem(THEME_STORAGE_KEY) || "light"; } catch (error) { /* use default */ }
        applyTheme(stored, false);
      });
    }
  } catch (error) {
    showError("bridge 初始化失败：" + errText(error));
  }

  try {
    await loadAll();
  } catch (error) {
    toast("加载失败：" + errText(error), "err");
  }
}

main();
