/** 异步视频设置页的离线回归测试；运行：node tests/test_video_settings.cjs。 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const nodes = new Map();

// 只实现被测渲染需要的 DOM 能力；保存、校验和默认值逻辑均运行原始源码。
class DomNode {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.attrs = {};
    this._value = '';
    this.checked = false;
    this.selected = false;
    this.parentNode = null;
    this.listeners = {};
    this.classList = { add() {}, remove() {} };
  }
  get firstChild() { return this.children[0] || null; }
  get childNodes() { return this.children; }
  get isConnected() { return Boolean(this.root || (this.parentNode && this.parentNode.isConnected)); }
  get value() {
    if (this.tagName === 'select') {
      return (this.children.find((node) => node.selected) || this.children[0] || {})._value || '';
    }
    return this._value;
  }
  set value(value) {
    this._value = String(value);
    if (this.tagName === "select") this.children.forEach((node) => { node.selected = node._value === this._value; });
  }
  setAttribute(key, value) {
    this.attrs[key] = value;
    if (key === 'id') nodes.set(value, this);
    if (key === 'checked' || key === 'selected') this[key] = true;
  }
  removeAttribute(key) {
    delete this.attrs[key];
    if (key === 'checked' || key === 'selected') this[key] = false;
  }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  async dispatch(type) {
    for (const handler of this.listeners[type] || []) await handler({ target: this, currentTarget: this, preventDefault() {} });
  }
  async click() { await this.dispatch("click"); }
  appendChild(node) { node.parentNode = this; this.children.push(node); return node; }
  removeChild(node) { this.children.splice(this.children.indexOf(node), 1); node.parentNode = null; }
}

const roots = ['models-body', 'videos-body', 'prompts-body'].map((id) => {
  const node = new DomNode('div');
  node.root = true;
  node.setAttribute('id', id);
  return node;
});
const modelRequests = [];
let requestFailure = false;
let heldResponse = null;
let holdNextResponse = false;
const fixtureModels = ['fixture-video-a', 'fixture-video-b', 'fixture-chat-model'];
const sandbox = {
  window: { AstrBotPluginPage: {
    apiGet: async (endpoint, params) => {
      modelRequests.push({ endpoint, params });
      if (requestFailure) throw new Error('fixture model directory failure');
      const response = { ok: true, data: { models: fixtureModels, total: fixtureModels.length } };
      if (holdNextResponse) {
        holdNextResponse = false;
        return new Promise((resolve) => { heldResponse = () => resolve(response); });
      }
      return response;
    },
  } },
  document: {
    createElement: (tag) => new DomNode(tag),
    createTextNode: (text) => Object.assign(new DomNode('#text'), { textContent: text }),
    getElementById: (id) => nodes.get(id)?.isConnected ? nodes.get(id) : null,
  },
  Node: DomNode,
  URL,
  console,
  setTimeout,
  clearTimeout,
};
const filename = path.join(root, 'pages/settings/app.js');
const source = fs.readFileSync(filename, 'utf8');
assert.match(source, /main\(\);\s*$/, '设置页启动入口发生变化，需要更新测试加载方式');
vm.createContext(sandbox);
vm.runInContext(source.replace(/main\(\);\s*$/, '') + `
  globalThis.testApi = {
    state, renderModels, renderVideos, renderPrompts, refreshSmartModelSources, collectPayload, validatePayload, applyAllDefaults,
    DEFAULT_COMMANDS, DEFAULT_SUPPLIER_COMMANDS, DEFAULT_PROMPTS,
  };
  markDirty = () => {};
  updateCompletion = () => {};
  renderAll = () => {};
  paintSummary = () => {};
  toast = () => {};
  createCollectionEditor = () => document.createElement("div");
`, sandbox, { filename });

const api = sandbox.testApi;
api.state.config = {};
api.applyAllDefaults();
assert.equal(api.state.config.video_api_mode, 'legacy');
assert.equal(api.state.config.video_model, '');
assert.deepEqual(Array.from(api.state.config.video_commands), ['生成视频', '视频生成', '文生视频', '图生视频']);
const schema = JSON.parse(fs.readFileSync(path.join(root, '_conf_schema.json'), 'utf8'));
assert.deepEqual(JSON.parse(JSON.stringify(api.state.config.menu_sections)), schema.menu_sections.default);
assert.ok(api.state.config.menu_sections[0].items.includes('生成视频'));
api.renderPrompts();
for (const key of ['video_start_prompt', 'video_progress_prompt', 'video_done_prompt']) {
  assert.equal(api.state.config[key], schema[key].default);
  assert.equal(api.state.promptTextareas[key].value, schema[key].default);
  assert.equal(api.DEFAULT_PROMPTS[key], schema[key].default);
}

api.renderModels();
api.renderVideos();
assert.equal(nodes.get('video-api-mode').value, 'legacy');
assert.equal(nodes.get('video-enabled').checked, true);
assert.equal(nodes.get('video-model').value, '');
for (const [id, expected] of [
  ['video-poll-interval', '5'], ['video-timeout', '900'], ['video-max-concurrent', '2'],
  ['video-progress-interval', '10'], ['video-duration-timeout', '120'],
]) assert.equal(nodes.get(id).value, expected);

api.state.commandEditors = Object.fromEntries(
  ['draw', 'edit', 'video', 'menu', 'master'].map((key) => [
    key + '_commands', { editor: { getValues: () => api.DEFAULT_COMMANDS[key] } },
  ]),
);
api.state.commandEditors.supplier_commands = {
  inputs: Array.from(api.DEFAULT_SUPPLIER_COMMANDS, (value) => ({ value })),
};
const payload = { ...api.collectPayload(), trigger_mode: 'at', cooldown: 0, retry_times: 1 };
assert.equal(api.validatePayload(payload), '');
assert.equal(payload.video_api_mode, 'legacy');
assert.equal(payload.video_poll_interval, 5);
assert.equal(payload.video_timeout, 900);
assert.equal(payload.video_model, "");
assert.equal(payload.video_progress_interval, 10);
assert.equal(payload.video_duration_timeout, 120);
for (const key of ["video_start_prompt", "video_progress_prompt", "video_done_prompt"]) {
  assert.equal(payload[key], schema[key].default);
}
assert.equal(payload.timeout, 600);
assert.deepEqual(Array.from(payload.video_commands), ['生成视频', '视频生成', '文生视频', '图生视频']);

const invalid = [
  ['video_api_mode', 'bad'], ['video_supplier', 'missing'],
  ['video_poll_interval', 1], ['video_poll_interval', 61], ['video_poll_interval', 2.5],
  ['video_timeout', 29], ['video_timeout', 3601],
  ['video_max_concurrent', 0], ['video_max_concurrent', 9], ['video_commands', []],
  ['video_progress_interval', 4], ['video_progress_interval', 61], ['video_progress_interval', 5.5],
  ['video_duration_timeout', 29], ['video_duration_timeout', 601], ['video_duration_timeout', 30.5],
];
for (const [key, value] of invalid) {
  assert.notEqual(api.validatePayload({ ...payload, [key]: value }), '', key);
}
assert.equal(api.validatePayload({ ...payload, video_api_mode: 'openai' }), '');
assert.equal(api.validatePayload({ ...payload, video_enabled: true, video_model: '' }), '');
api.state.promptTextareas.video_progress_prompt.value = '自定义 {progress_type}{progress}%';
assert.equal(api.collectPayload().video_progress_prompt, '自定义 {progress_type}{progress}%');
api.state.promptTextareas.video_progress_prompt.value = '';
assert.equal(api.collectPayload().video_progress_prompt, '');

api.state.config.video_supplier = '已删除的供应商';
api.renderModels();
api.renderVideos();
assert.equal(nodes.get('video-supplier').value, '已删除的供应商');
assert.equal(api.collectPayload().video_supplier, '已删除的供应商');
api.state.config.video_enabled = false;
api.renderModels();
api.renderVideos();
assert.equal(nodes.get('video-enabled').checked, false);
assert.equal(api.collectPayload().video_enabled, false);

function descendants(node) {
  return [node, ...node.children.flatMap(descendants)];
}
function allNodes() { return roots.flatMap(descendants); }
function buttonsForModels() {
  return descendants(nodes.get('videos-body')).filter((node) => (node.className || '').split(' ').includes('model-item'));
}
function textOf(node) { return (node.textContent || '') + node.children.map(textOf).join(''); }

(async () => {
  // 视频控件只属于独立视频页，模型页不再存在第二份输入框。
  const coreIds = ['video-model', 'video-enabled', 'video-supplier', 'video-api-mode', 'video-poll-interval', 'video-progress-interval', 'video-duration-timeout', 'video-timeout', 'video-max-concurrent'];
  const imageNodes = descendants(nodes.get('models-body'));
  for (const id of coreIds) {
    assert.equal(allNodes().filter((node) => node.attrs.id === id).length, 1, id);
    assert.equal(imageNodes.filter((node) => node.attrs.id === id).length, 0, id);
  }
  const ids = allNodes().map((node) => node.attrs.id).filter(Boolean);
  assert.equal(new Set(ids).size, ids.length, '渲染后 ID 不能重复');
  const html = fs.readFileSync(path.join(root, 'pages/settings/index.html'), 'utf8');
  assert.match(html, /id="section-videos"/);
  assert.match(html, /id="videos-body"/);
  assert.match(html, /href="\.\/style\.css\?v=1\.0\.10"/);
  assert.match(html, /src="\.\/app\.js\?v=1\.0\.10&rev=supplier-models-1"/);
  assert.match(source, /FALLBACK_VERSION = "1\.0\.10"/);

  api.state.config.video_supplier = '';
  api.state.suppliers.push({ name: '专用视频供应商', base_url: 'https://example.invalid/v1' });
  api.state.activeProtocol = 'gemini';
  api.renderVideos();
  const imageModels = JSON.stringify(api.state.protocolModels);
  await nodes.get('video-model-fetch').click();
  assert.equal(modelRequests.at(-1).endpoint, 'models');
  assert.equal(modelRequests.at(-1).params.supplier, api.state.config.active_supplier);
  assert.equal(modelRequests.at(-1).params.protocol, 'openai');
  assert.equal(modelRequests.at(-1).params.include_all, 1);
  assert.equal(modelRequests.at(-1).params.refresh, 1);
  assert.equal(Object.hasOwn(modelRequests.at(-1).params, 'purpose'), false);
  assert.equal(buttonsForModels().length, 3);
  nodes.get('video-model-search').value = 'video-b';
  await nodes.get('video-model-search').dispatch('input');
  assert.equal(buttonsForModels().length, 1);
  assert.match(textOf(buttonsForModels()[0]), /fixture-video-b/);
  await buttonsForModels()[0].click();
  assert.equal(nodes.get('video-model').value, 'fixture-video-b');
  assert.equal(api.state.config.video_model, 'fixture-video-b');
  assert.equal(api.collectPayload().video_model, 'fixture-video-b');
  assert.equal(JSON.stringify(api.state.protocolModels), imageModels);
  assert.equal(api.state.activeProtocol, 'gemini');

  nodes.get('video-supplier').value = '专用视频供应商';
  await nodes.get('video-supplier').dispatch('change');
  await nodes.get('video-model-fetch').click();
  assert.equal(modelRequests.at(-1).params.supplier, '专用视频供应商');
  assert.equal(api.collectPayload().video_supplier, '专用视频供应商');
  nodes.get('video-model').value = 'manually-entered-video';
  await nodes.get('video-model').dispatch('input');
  assert.equal(api.collectPayload().video_model, 'manually-entered-video');

  // 获取失败不影响手动填写；改变供应商后丢弃此前未完成的返回。
  requestFailure = true;
  await nodes.get('video-model-fetch').click();
  assert.equal(api.collectPayload().video_model, 'manually-entered-video');
  requestFailure = false;
  holdNextResponse = true;
  const pending = nodes.get('video-model-fetch').click();
  await Promise.resolve();
  nodes.get('video-supplier').value = '';
  await nodes.get('video-supplier').dispatch('change');
  assert.equal(typeof heldResponse, 'function');
  heldResponse();
  await pending;
  assert.equal(api.collectPayload().video_supplier, '');
  assert.equal(api.collectPayload().video_model, 'manually-entered-video');
  assert.equal(JSON.stringify(api.state.protocolModels), imageModels);

  console.log('PASS: 独立视频分区、唯一 ID、模型获取/筛选/选择/手填、供应商切换、陈旧响应丢弃、图片设置隔离、模板及 ' + invalid.length + ' 项非法输入');
})().catch((error) => { console.error(error); process.exitCode = 1; });
