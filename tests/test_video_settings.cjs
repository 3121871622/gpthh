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
  }
  get firstChild() { return this.children[0] || null; }
  get childNodes() { return this.children; }
  get value() {
    if (this.tagName === 'select') {
      return (this.children.find((node) => node.selected) || this.children[0] || {})._value || '';
    }
    return this._value;
  }
  set value(value) { this._value = String(value); }
  setAttribute(key, value) {
    this.attrs[key] = value;
    if (key === 'id') nodes.set(value, this);
    if (key === 'checked' || key === 'selected') this[key] = true;
  }
  removeAttribute(key) {
    delete this.attrs[key];
    if (key === 'checked' || key === 'selected') this[key] = false;
  }
  addEventListener() {}
  appendChild(node) { this.children.push(node); return node; }
  removeChild(node) { this.children.splice(this.children.indexOf(node), 1); }
}

nodes.set('models-body', new DomNode('div'));
nodes.set('prompts-body', new DomNode('div'));
const sandbox = {
  window: {},
  document: {
    createElement: (tag) => new DomNode(tag),
    createTextNode: (text) => Object.assign(new DomNode('#text'), { textContent: text }),
    getElementById: (id) => nodes.get(id) || null,
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
    state, renderModels, renderPrompts, collectPayload, validatePayload, applyAllDefaults,
    DEFAULT_COMMANDS, DEFAULT_SUPPLIER_COMMANDS, DEFAULT_PROMPTS,
  };
  markDirty = () => {};
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
assert.equal(nodes.get('video-supplier').value, '已删除的供应商');
assert.equal(api.collectPayload().video_supplier, '已删除的供应商');
api.state.config.video_enabled = false;
api.renderModels();
assert.equal(nodes.get('video-enabled').checked, false);
assert.equal(api.collectPayload().video_enabled, false);

console.log('PASS: 视频空模型、四种指令、三种模板渲染/保存、参数范围、菜单一致性、' + invalid.length + ' 项非法输入及视频开关');
