/** 模型供应商自动发现的真实整页回归：只连接 localhost，不发送生成请求。 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const pageRoot = process.env.TEST_PAGE_ROOT || path.join(root, 'pages/settings');
const TARGET = 'fixture-target';
const names = ['First', '22', 'Case provider', 'Broken provider', 'Last provider'];
const terminal = ['matched', 'case_mismatch', 'not_found', 'catalog_unavailable', 'auth_failed', 'timeout', 'network_error', 'unsaved', 'error'];
const baseConfig = JSON.parse(execFileSync(process.env.TEST_PYTHON || 'python', ['-B', '-c',
  'import json; from core.config import PluginConfig; print(json.dumps(PluginConfig({}).to_public_dict()))'], {cwd: root, encoding: 'utf8'}));
baseConfig.suppliers = names.map((name, index) => ({name, base_url: `https://supplier-${index + 1}.invalid/v1`, api_key_set: true, api_key_masked: 'configured'}));
baseConfig.active_supplier = names[0];
baseConfig.active_protocol = 'openai';
baseConfig.protocol_models.openai = {model: 'fixture-initial', edit_model: ''};

let fixture;
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
function resetFixture() {
  fixture = {
    saved: structuredClone(baseConfig), calls: [], active: 0, maxActive: 0, delay: 55,
    saveFailure: false, broken: {}, custom: null,
    catalogs: Object.fromEntries(names.map(name => [name, [TARGET, 'fixture-chip', 'fixture-edit']])),
  };
  return fixture;
}
function diagnostic(f, query, override = {}) {
  const catalog = f.catalogs[query.supplier] || [];
  const exact = catalog.includes(query.model);
  const canonical = exact ? query.model : catalog.find(name => name.toLowerCase() === query.model.toLowerCase()) || '';
  return {
    ok: true, ...query, status: exact ? 'matched' : canonical ? 'case_mismatch' : 'not_found',
    connection: 'ok', matched: exact, canonical_model: canonical, catalog_total: catalog.length,
    catalog_complete: true, cached: false, elapsed_ms: 12,
    message: exact ? '目录存在，未验证实际出图' : canonical ? '模型 ID 大小写不同' : '完整目录未找到该模型',
    generation_verified: false, ...override,
  };
}
function publishSavedConfig(f, payload) {
  const previous = f.saved.suppliers;
  f.saved = {...f.saved, ...payload};
  f.saved.suppliers = payload.suppliers.map(entry => {
    const old = previous.find(item => item.name === entry.name);
    const keySet = entry.api_key_clear ? false : Boolean(entry.api_key || old?.api_key_set);
    const result = {...entry, api_key_set: keySet, api_key_masked: keySet ? 'configured' : ''};
    delete result.api_key;
    delete result.api_key_clear;
    return result;
  });
}
const bridge = `<script>
const bridgeResponse=async(response)=>{const data=await response.json();if(!response.ok||data?.status==='error')throw new Error(data?.message||'Bridge request failed');return data?.data??data;};
window.AstrBotPluginPage={ready:async()=>({}),onContext:()=>{},apiGet:async(p,a)=>fetch('/_bridge/'+p+'?'+new URLSearchParams(a||{})).then(bridgeResponse),apiPost:async(p,b)=>fetch('/_bridge/'+p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(bridgeResponse)};
</script>`;
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname.startsWith('/_bridge/')) {
    const f = fixture;
    const endpoint = url.pathname.slice(9);
    let payload = {};
    if (req.method === 'POST') {let text = ''; for await (const chunk of req) text += chunk; payload = text ? JSON.parse(text) : {};}
    const query = Object.fromEntries(url.searchParams);
    const record = {endpoint, method: req.method, query, payload, started: Date.now()};
    f.calls.push(record);
    let data = {};
    if (endpoint === 'suppliers/model-check') {
      f.active += 1;
      f.maxActive = Math.max(f.maxActive, f.active);
      try {
        await delay(typeof f.delay === 'function' ? f.delay(query) : f.delay);
        const broken = f.broken[query.supplier];
        if (broken === 'http') {res.writeHead(503, {'Content-Type': 'application/json'}); res.end(JSON.stringify({message: 'fixture service unavailable'})); return;}
        if (broken === 'json') {res.writeHead(200, {'Content-Type': 'application/json'}); res.end('{fixture-invalid-json'); return;}
        data = f.custom ? await f.custom(query, f) : diagnostic(f, query);
      } finally {record.finished = Date.now(); f.active -= 1;}
    } else if (endpoint === 'config') {
      if (req.method === 'POST') {
        if (f.saveFailure) data = {ok: false, saved: false, message: 'fixture-save-failed'};
        else {publishSavedConfig(f, payload); data = {ok: true, saved: true, config_reloaded: true};}
      } else data = f.saved;
    } else if (endpoint === 'suppliers') data = {suppliers: f.saved.suppliers};
    else if (endpoint === 'status') data = {version: '1.0.10', ready: true, build: 100};
    else if (endpoint === 'models') {
      const supplier = query.supplier || f.saved.active_supplier;
      data = {ok: true, supplier, protocol: query.protocol || 'openai', models: f.catalogs[supplier] || []};
    }
    res.writeHead(200, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'});
    res.end(JSON.stringify({ok: data.ok !== false, data}));
    return;
  }
  const file = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
  if (!['index.html', 'app.js', 'style.css'].includes(file)) {res.writeHead(404); res.end(); return;}
  let body = fs.readFileSync(path.join(pageRoot, file), 'utf8');
  if (file === 'index.html') body = body.replace('<head>', '<head>' + bridge);
  res.writeHead(200, {'Content-Type': file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html', 'Cache-Control': 'no-store'});
  res.end(body);
});

const checks = f => f.calls.filter(call => call.endpoint === 'suppliers/model-check');
const configWrites = f => f.calls.filter(call => call.endpoint === 'config' && call.method === 'POST');
async function rowsDone(page, count = 5) {
  await page.waitForFunction(({count, terminal}) => {
    const rows = [...document.querySelectorAll('#model-discovery-results [data-supplier][data-status]')];
    return rows.length === count && rows.every(row => terminal.includes(row.dataset.status));
  }, {count, terminal}, {timeout: 20000});
}
async function statusOf(page, supplier) {
  return page.locator(`#model-discovery-results [data-supplier=${JSON.stringify(supplier)}][data-status]`).getAttribute('data-status');
}
async function waitCalls(f, count) {
  const until = Date.now() + 10000;
  while (checks(f).length < count && Date.now() < until) await delay(20);
  assert.ok(checks(f).length >= count, `预期至少 ${count} 个检测请求，实际 ${checks(f).length}`);
}
async function save(page, f) {
  const before = configWrites(f).length;
  await page.locator('#save-btn').click();
  const until = Date.now() + 10000;
  while (configWrites(f).length === before && Date.now() < until) await delay(20);
  assert.equal(configWrites(f).length, before + 1, '保存按钮没有提交配置');
  await page.waitForFunction(() => !document.querySelector('#save-btn').disabled);
}
function noImplicitWrites(f) {
  assert.equal(configWrites(f).length, 0, '检测不得自动保存配置');
  assert.equal(f.saved.active_supplier, names[0], '检测不得自动切换已保存供应商');
  assert.equal(f.saved.protocol_models.openai.model, 'fixture-initial', '检测不得写入服务端模型');
  assert.equal(f.calls.filter(call => /update-install|plugin-reload|images\/|videos/.test(call.endpoint)).length, 0, '检测不得启动生成、更新或重载任务');
}

async function saveDiscoveryPreview(page) {
  if (!process.env.MODEL_DISCOVERY_SCREENSHOT) return;
  const sharp = require('sharp');
  const target = path.resolve(process.env.MODEL_DISCOVERY_SCREENSHOT);
  const card = page.locator('.card').filter({has: page.locator('#protocol-model')});
  // 固定保存栏属于卡片外；仅在截图期间隐藏，避免遮住第五家检测结果。
  const original = await card.screenshot({type: 'jpeg', quality: 80, animations: 'disabled', style: '.sticky-actions { visibility: hidden !important; }'});
  let quality = 78;
  let preview;
  do {
    preview = await sharp(original).resize({width: 1280, height: 1280, fit: 'inside', withoutEnlargement: true}).jpeg({quality}).toBuffer();
    quality -= 10;
  } while (preview.length > 500 * 1024 && quality >= 38);
  const metadata = await sharp(preview).metadata();
  assert.ok(metadata.width <= 1280 && metadata.height <= 1280, '预览最长边必须不超过 1280 像素');
  assert.ok(preview.length <= 500 * 1024, '预览必须不超过 500 KiB');
  fs.mkdirSync(path.dirname(target), {recursive: true});
  fs.writeFileSync(target, preview);
  console.log(`SCREENSHOT: ${target} | ${metadata.width}x${metadata.height} | ${preview.length} bytes`);
}

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless: true, channel: process.env.TEST_BROWSER_CHANNEL || 'msedge'});
  const baseUrl = `http://127.0.0.1:${server.address().port}/`;
  const failed = [];
  let passed = 0;
  async function runCase(name, callback) {
    if (process.env.TEST_CASE && !name.includes(process.env.TEST_CASE)) return;
    const f = resetFixture();
    const page = await browser.newPage({viewport: {width: 1280, height: 900}});
    page.setDefaultTimeout(10000);
    const pageErrors = [];
    page.on('pageerror', error => pageErrors.push(error.message));
    page.on('dialog', dialog => dialog.accept());
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.hostname !== '127.0.0.1') return route.abort();
      return route.continue();
    });
    try {
      await page.goto(baseUrl);
      await page.waitForFunction(() => document.querySelectorAll('.supplier-card').length === 5);
      await page.getByRole('tab', {name: '供应商与协议', exact: true}).click();
      await callback(page, f);
      assert.deepEqual(pageErrors, [], '页面出现未处理 JavaScript 错误');
      assert.ok(f.maxActive <= 2, `全局检测并发超限：${f.maxActive}`);
      passed += 1; console.log('PASS: ' + name);
    } catch (error) {
      failed.push(name);
      console.error('FAIL: ' + name + '\n' + error.message.split('\n').slice(0, 7).join('\n'));
    } finally {await page.close();}
  }
  try {
    await runCase('挂载与防抖：加载不请求、连续输入只检测最终值、清空取消', async (page, f) => {
      assert.equal(await page.locator('#model-discovery-panel').count(), 1, '缺少自动发现面板');
      await page.waitForTimeout(850); assert.equal(checks(f).length, 0);
      await page.locator('#protocol-model').fill('fix'); await page.waitForTimeout(80);
      await page.locator('#protocol-model').fill(TARGET); await page.waitForTimeout(450);
      assert.equal(checks(f).length, 0, '700ms 防抖前不应发请求');
      await rowsDone(page);
      assert.equal(checks(f).length, 5);
      assert.ok(checks(f).every(call => call.query.model === TARGET));
      await page.locator('#protocol-model').fill('temporary');
      await page.locator('#protocol-model').fill('');
      await page.waitForTimeout(850); assert.equal(checks(f).length, 5);
      noImplicitWrites(f);
    });
    await runCase('五家有界并发、请求来源准确且绝不自动切换或保存', async (page, f) => {
      f.delay = 180;
      await page.locator('#protocol-model').fill(TARGET); await rowsDone(page);
      assert.deepEqual(checks(f).map(call => call.query.supplier).sort(), [...names].sort());
      assert.ok(checks(f).every(call => call.method === 'GET' && call.query.protocol === 'openai' && call.query.purpose === 'generate' && call.query.model === TARGET));
      assert.equal(await page.locator('.supplier-card').nth(0).locator('input[name="active-supplier"]').isChecked(), true);
      assert.equal(await page.locator('#protocol-model').inputValue(), TARGET);
      noImplicitWrites(f);
      await saveDiscoveryPreview(page);
    });
    await runCase('精确匹配与大小写建议不自动改名，目录命中不冒充已出图', async (page, f) => {
      f.catalogs[names[1]] = [TARGET + '-plus'];
      f.catalogs[names[2]] = [TARGET.toUpperCase()];
      await page.locator('#protocol-model').fill('  ' + TARGET + '  '); await rowsDone(page);
      assert.equal(await statusOf(page, names[0]), 'matched');
      assert.equal(await statusOf(page, names[1]), 'not_found');
      assert.equal(await statusOf(page, names[2]), 'case_mismatch');
      assert.equal(await page.locator('#protocol-model').inputValue(), '  ' + TARGET + '  ');
      assert.ok(checks(f).every(call => call.query.model === TARGET));
      const text = await page.locator('#model-discovery-panel').textContent();
      assert.match(text, /(?:未验证|不代表|不等于|不保证|不会|仅).{0,50}(?:出图|生成)|(?:出图|生成).{0,30}(?:未验证|不保证)/);
      noImplicitWrites(f);
      await page.locator(`#model-discovery-results [data-supplier=${JSON.stringify(names[2])}] button[data-action="use-model-id"]`).click();
      assert.equal(await page.locator('#protocol-model').inputValue(), TARGET.toUpperCase());
      assert.equal(f.saved.active_supplier, names[0]);
    });
    await runCase('旧模型迟到响应不覆盖新模型，跨轮次仍最多两个请求', async (page, f) => {
      f.delay = query => query.model === 'old-model' ? 1800 : 40;
      f.custom = query => diagnostic(f, query, {status: query.model === 'old-model' ? 'matched' : 'not_found', matched: query.model === 'old-model', message: query.model === 'old-model' ? 'OLD-MODEL-ONLY' : 'NEW-MODEL-ONLY'});
      await page.locator('#protocol-model').fill('old-model'); await waitCalls(f, 2);
      await page.locator('#protocol-model').fill('new-model');
      await page.waitForFunction(() => [...document.querySelectorAll('#model-discovery-results [data-status]')].filter(node => node.dataset.status === 'not_found').length === 5, null, {timeout: 20000});
      await page.waitForTimeout(300);
      assert.doesNotMatch(await page.locator('#model-discovery-results').textContent(), /OLD-MODEL-ONLY/);
      assert.equal(await page.locator('#protocol-model').inputValue(), 'new-model'); noImplicitWrites(f);
    });
    await runCase('切换协议使旧结果失效，不混用 OpenAI 与 Gemini 响应', async (page, f) => {
      f.delay = query => query.protocol === 'openai' ? 1500 : 40;
      f.custom = query => diagnostic(f, query, {status: query.protocol === 'gemini' ? 'matched' : 'not_found', matched: query.protocol === 'gemini', message: query.protocol === 'gemini' ? 'GEMINI-CURRENT' : 'OPENAI-STALE'});
      await page.locator('#protocol-model').fill(TARGET); await waitCalls(f, 2);
      await page.getByRole('radio', {name: /Gemini/}).click();
      await page.locator('#protocol-model').fill(TARGET);
      await page.waitForFunction(() => [...document.querySelectorAll('#model-discovery-results [data-status]')].filter(node => node.dataset.status === 'matched').length === 5, null, {timeout: 20000});
      assert.doesNotMatch(await page.locator('#model-discovery-results').textContent(), /OPENAI-STALE/);
      assert.equal(checks(f).filter(call => call.query.protocol === 'gemini').length, 5); noImplicitWrites(f);
    });
    await runCase('地址、密钥、名称与清空密钥未保存时只跳过变更供应商', async (page, f) => {
      const cards = page.locator('.supplier-card');
      await cards.nth(0).locator('input[type="text"]').nth(1).fill('https://changed.invalid/v1');
      await cards.nth(1).locator('input[type="password"]').fill('fixture-new-key');
      await cards.nth(2).locator('input[type="text"]').nth(0).fill('Renamed provider');
      await cards.nth(3).getByRole('button', {name: /清空密钥/}).click();
      await page.getByRole('button', {name: '确认清空', exact: true}).click();
      await page.locator('#protocol-model').fill(TARGET); await rowsDone(page);
      for (const name of [names[0], names[1], 'Renamed provider', names[3]]) assert.equal(await statusOf(page, name), 'unsaved');
      assert.deepEqual(checks(f).map(call => call.query.supplier), [names[4]]); noImplicitWrites(f);
    });
    await runCase('新增及重名供应商跳过检测，不错误命中已保存的同名配置', async (page, f) => {
      await page.locator('.supplier-card').nth(2).locator('input[type="text"]').first().fill(names[0]);
      await page.getByRole('button', {name: /新增供应商/}).first().click();
      const added = page.locator('.supplier-card').last();
      await added.locator('input[type="text"]').nth(0).fill('New provider');
      await added.locator('input[type="text"]').nth(1).fill('https://new.invalid/v1');
      await added.locator('input[type="password"]').fill('fixture-new-key');
      await page.locator('#protocol-model').fill(TARGET); await rowsDone(page, 6);
      assert.equal(await statusOf(page, 'New provider'), 'unsaved');
      const duplicates = await page.locator(`#model-discovery-results [data-supplier=${JSON.stringify(names[0])}]`).evaluateAll(rows => rows.map(row => row.dataset.status));
      assert.deepEqual(duplicates, ['unsaved', 'unsaved']);
      assert.ok(checks(f).every(call => ![names[0], 'New provider'].includes(call.query.supplier))); noImplicitWrites(f);
    });
    await runCase('单家鉴权、超时、网络与目录失败隔离，负面业务状态不变成功', async (page, f) => {
      const states = ['matched', 'auth_failed', 'timeout', 'network_error', 'catalog_unavailable'];
      f.custom = query => {
        const status = states[names.indexOf(query.supplier)];
        return diagnostic(f, query, {status, ok: status === 'matched', connection: status === 'matched' ? 'ok' : status, matched: status === 'matched' ? true : null, cached: status === 'catalog_unavailable', message: status === 'matched' ? '目录存在，未验证实际出图' : 'FIXTURE-' + status});
      };
      await page.locator('#protocol-model').fill(TARGET); await rowsDone(page);
      for (let index = 0; index < names.length; index += 1) assert.equal(await statusOf(page, names[index]), states[index]);
      assert.equal(checks(f).length, 5); noImplicitWrites(f);
    });
    for (const mode of ['http', 'json', 'wrong-source']) {
      await runCase('异常响应隔离：' + mode, async (page, f) => {
        if (mode === 'wrong-source') f.custom = query => diagnostic(f, query, query.supplier === names[1] ? {supplier: names[0]} : {});
        else f.broken[names[1]] = mode;
        await page.locator('#protocol-model').fill(TARGET); await rowsDone(page);
        assert.notEqual(await statusOf(page, names[1]), 'matched', '异常或错误来源响应不得标记目录命中');
        for (const name of names.filter(name => name !== names[1])) assert.equal(await statusOf(page, name), 'matched');
        assert.equal(checks(f).length, 5); noImplicitWrites(f);
      });
    }
    await runCase('停止后不启动剩余队列，迟到结果失效且可手动重新检测', async (page, f) => {
      f.delay = 900;
      await page.locator('#protocol-model').fill(TARGET); await waitCalls(f, 2);
      await page.locator('#model-discovery-stop').click();
      await page.waitForTimeout(1150);
      assert.equal(checks(f).length, 2, '停止后仍启动了后续供应商请求');
      assert.match(await page.locator('#model-discovery-status').textContent(), /停止|取消/);
      f.delay = 30;
      await page.locator('#model-discovery-check').click(); await rowsDone(page);
      assert.equal(checks(f).length, 7); noImplicitWrites(f);
    });
    await runCase('保存失败保留未保存隔离，成功保存后重新检测并保留刷新状态', async (page, f) => {
      await page.locator('.supplier-card').first().locator('input[type="text"]').nth(1).fill('https://changed.invalid/v1');
      await page.locator('#protocol-model').fill(TARGET); await rowsDone(page);
      assert.equal(await statusOf(page, names[0]), 'unsaved');
      f.saveFailure = true; await save(page, f);
      assert.equal(await page.locator('#dirty-flag').isVisible(), true);
      await page.locator('#model-discovery-check').click(); await rowsDone(page);
      assert.equal(await statusOf(page, names[0]), 'unsaved');
      assert.equal(checks(f).filter(call => call.query.supplier === names[0]).length, 0);
      f.saveFailure = false; await save(page, f);
      assert.equal(f.saved.suppliers[0].base_url, 'https://changed.invalid/v1');
      await page.locator('#model-discovery-check').click(); await rowsDone(page);
      assert.equal(await statusOf(page, names[0]), 'matched');
      assert.equal(checks(f).filter(call => call.query.supplier === names[0]).length, 1);
      const beforeReload = checks(f).length;
      await page.reload(); await page.waitForFunction(() => document.querySelectorAll('.supplier-card').length === 5);
      await page.waitForTimeout(850);
      assert.equal(checks(f).length, beforeReload, '重新加载页面不应自动启动检测');
      assert.equal(await page.locator('#protocol-model').inputValue(), TARGET);
    });
    await runCase('用户手动使用供应商仅改草稿，保存与页面重载后才持久生效', async (page, f) => {
      await page.locator('#protocol-model').fill(TARGET); await rowsDone(page);
      await page.locator(`#model-discovery-results [data-supplier=${JSON.stringify(names[1])}] button[data-action="select-supplier"]`).click();
      assert.equal(await page.locator('.supplier-card').nth(1).locator('input[name="active-supplier"]').isChecked(), true);
      assert.equal(f.saved.active_supplier, names[0]); assert.equal(configWrites(f).length, 0);
      assert.equal(await page.locator('#protocol-model').inputValue(), TARGET);
      await save(page, f); assert.equal(f.saved.active_supplier, names[1]);
      await page.reload(); await page.waitForFunction(() => document.querySelectorAll('.supplier-card').length === 5);
      assert.equal(await page.locator('.supplier-card').nth(1).locator('input[name="active-supplier"]').isChecked(), true);
      assert.equal(f.calls.filter(call => /update-install|plugin-reload/.test(call.endpoint)).length, 0);
    });
    await runCase('模型标签点选接入同一自动检测调度器', async (page, f) => {
      await page.locator('#protocol-model').focus();
      const card = page.locator('.card').filter({has: page.locator('#protocol-model')});
      await card.getByRole('button', {name: /获取.*模型列表/}).click();
      await card.locator('.model-item').filter({hasText: 'fixture-chip'}).click();
      await rowsDone(page);
      assert.equal(await page.locator('#protocol-model').inputValue(), 'fixture-chip');
      assert.ok(checks(f).every(call => call.query.model === 'fixture-chip' && call.query.purpose === 'generate'));
      noImplicitWrites(f);
    });
    await runCase('编辑模型检测携带 edit 用途，不覆盖生成模型', async (page, f) => {
      await page.locator('#protocol-edit-model').fill('fixture-edit'); await rowsDone(page);
      assert.ok(checks(f).every(call => call.query.model === 'fixture-edit' && call.query.purpose === 'edit'));
      assert.equal(await page.locator('#protocol-model').inputValue(), 'fixture-initial'); noImplicitWrites(f);
    });
    await runCase('390px 手机无横向溢出，检测停止与供应商选择按钮均可点击', async (page, f) => {
      await page.setViewportSize({width: 390, height: 844});
      await page.locator('#protocol-model').fill(TARGET); await rowsDone(page);
      const dimensions = await page.evaluate(() => {
        const panel = document.querySelector('#model-discovery-panel');
        const card = document.querySelector('#protocol-model').closest('.card');
        return {
          viewport: window.innerWidth, document: document.documentElement.scrollWidth,
          body: document.body.scrollWidth, panelWidth: panel.clientWidth, panelScroll: panel.scrollWidth,
          cardWidth: card.clientWidth, cardScroll: card.scrollWidth,
        };
      });
      assert.ok(dimensions.document <= dimensions.viewport + 1 && dimensions.body <= dimensions.viewport + 1,
        '手机整页横向溢出：' + JSON.stringify(dimensions));
      assert.ok(dimensions.panelScroll <= dimensions.panelWidth + 1 && dimensions.cardScroll <= dimensions.cardWidth + 1,
        '手机模型面板横向溢出：' + JSON.stringify(dimensions));
      f.delay = 700;
      const before = checks(f).length;
      await page.locator('#model-discovery-check').click(); await waitCalls(f, before + 2);
      await page.locator('#model-discovery-stop').click(); await page.waitForTimeout(900);
      assert.equal(checks(f).length, before + 2, '手机停止按钮未阻止后续请求');
      f.delay = 30;
      await page.locator('#model-discovery-check').click(); await rowsDone(page);
      await page.locator(`#model-discovery-results [data-supplier=${JSON.stringify(names[1])}] button[data-action="select-supplier"]`).click();
      assert.equal(await page.locator('.supplier-card').nth(1).locator('input[name="active-supplier"]').isChecked(), true);
      noImplicitWrites(f);
    });
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
  console.log(`RESULT: ${passed} passed, ${failed.length} failed`);
  if (failed.length) process.exitCode = 1;
})().catch(error => {console.error(error); server.close(); process.exitCode = 1;});
