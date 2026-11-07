/** 供应商模型来源与真实 AstrBot 解包契约回归；不访问网络。 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../pages/settings/app.js'), 'utf8');
let response, requested;
const sandbox = {
  window: {AstrBotPluginPage: {apiGet: async (endpoint, params) => {
    requested = {endpoint, params};
    // 与官方父页一致，向插件返回已解包的一层 data。
    return response.data ?? response;
  }}},
  document: {}, URL, console, setTimeout, clearTimeout,
};
vm.createContext(sandbox);
vm.runInContext(source.replace(/main\(\);\s*$/, '') + `
  globalThis.testApi = {apiGet, state, loadModelCache, modelCacheKey, readModelsResponse};
`, sandbox);
const api = sandbox.testApi;
const plain = value => JSON.parse(JSON.stringify(value));
(async () => {
  const first = '供应商一', second = '供应商二';
  response = {ok: true, data: {ok: true, supplier: first, protocol: 'openai', models: ['only-one']}};
  const mismatch = await api.apiGet('models', {supplier: second, protocol: 'openai', refresh: 1});
  assert.equal(requested.params.supplier, second);
  assert.equal(mismatch.ok, false, '不得把一号响应标成二号模型');
  assert.deepEqual(plain(mismatch.models), []);
  assert.match(mismatch.message, /供应商|来源/);
  response = {ok: true, data: {ok: true, supplier: second, protocol: 'gemini', models: ['wrong-protocol']}};
  assert.equal((await api.apiGet('models', {supplier: second, protocol: 'openai'})).ok, false);
  response = {ok: true, data: {ok: true, supplier: second, protocol: 'openai', models: ['only-two']}};
  const valid = await api.apiGet('models', {supplier: second, protocol: 'openai'});
  assert.equal(valid.ok, true);
  assert.deepEqual(plain(valid.models), ['only-two']);
  response = {ok: false, data: {ok: false, supplier: second, protocol: 'openai', models: ['only-two-cached'], cached: true, message: 'fixture upstream failed'}};
  const failed = await api.apiGet('models', {supplier: second, protocol: 'openai'});
  assert.equal(failed.ok, false);
  assert.equal(failed.cached, true);
  assert.deepEqual(plain(failed.models), ['only-two-cached']);
  api.loadModelCache({model_cache: {openai: ['unknown-origin']}}, first);
  assert.deepEqual(plain(api.state.modelCache), {}, '旧版只有协议的缓存不得归给当前供应商');
  const firstKey = api.modelCacheKey(first, 'openai'), secondKey = api.modelCacheKey(second, 'openai');
  api.loadModelCache({model_cache: {[firstKey]: ['only-one'], [secondKey]: ['only-two']}}, first);
  assert.deepEqual(plain(api.state.modelCache[firstKey]), ['only-one']);
  assert.deepEqual(plain(api.state.modelCache[secondKey]), ['only-two']);
  console.log('PASS: 模型来源核对、协议隔离、真实宿主解包失败标记、旧缓存隔离');
})().catch(error => {console.error(error); process.exitCode = 1;});
