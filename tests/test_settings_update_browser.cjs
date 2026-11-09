/** 真实设置页供应商切换、保存错误和更新/重载异步任务回归；只连接本机 HTTP fixture。 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const pageRoot = process.env.TEST_PAGE_ROOT || path.join(root, 'pages/settings');
const baseConfig = JSON.parse(execFileSync(process.env.TEST_PYTHON || 'python', ['-B', '-c',
  'import json; from core.config import PluginConfig; print(json.dumps(PluginConfig({}).to_public_dict()))'],
  {cwd: root, encoding: 'utf8'}));
baseConfig.suppliers = ['First', '22'].map((name, index) => ({
  name, base_url: `https://supplier-${index + 1}.invalid/v1`, api_key_set: true, api_key_masked: 'configured',
}));
baseConfig.active_supplier = 'First';
baseConfig.active_protocol = 'openai';
baseConfig.protocol_models.openai = {model: 'fixture-image', edit_model: 'fixture-image'};
const catalogs = {First: ['first-image-model'], '22': ['second-image-model']};
let saved, calls, saveFailure, terminalStatus, pollCount, currentJob, statusBusinessFailure;
function resetFixture() {
  saved = structuredClone(baseConfig);
  calls = [];
  saveFailure = false;
  terminalStatus = 'succeeded';
  pollCount = 0;
  currentJob = '';
  statusBusinessFailure = false;
}
resetFixture();
const bridge = `<script>
const bridgeResponse=async(response)=>{const data=await response.json();if(!response.ok||data?.status==='error')throw new Error(data?.message||'Bridge request failed');return data?.data??data;};
window.AstrBotPluginPage={ready:async()=>({}),onContext:()=>{},apiGet:async(p,a)=>fetch('/_bridge/'+p+'?'+new URLSearchParams(a||{})).then(bridgeResponse),apiPost:async(p,b)=>fetch('/_bridge/'+p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(bridgeResponse)};
</script>`;
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname.startsWith('/_bridge/')) {
    const endpoint = url.pathname.slice(9);
    let payload = {};
    if (req.method === 'POST') {
      let body = ''; for await (const chunk of req) body += chunk;
      payload = body ? JSON.parse(body) : {};
    }
    calls.push({endpoint, method: req.method, params: Object.fromEntries(url.searchParams), payload});
    let data = {};
    if (endpoint === 'config') {
      if (req.method === 'POST') {
        if (saveFailure) data = {ok: false, saved: false, message: 'fixture-config-save-failed'};
        else {saved = {...saved, ...payload}; data = {ok: true, saved: true, reloaded: true};}
      } else data = saved;
    } else if (endpoint === 'suppliers') data = {suppliers: saved.suppliers};
    else if (endpoint === 'status') data = {version: '1.1.0'};
    else if (endpoint === 'models') {
      const supplier = url.searchParams.get('supplier') || saved.active_supplier;
      data = {ok: true, supplier, protocol: url.searchParams.get('protocol') || 'openai', models: catalogs[supplier] || []};
    } else if (endpoint === 'update-check') {
      data = {ok: true, update_available: true, current_version: '1.1.0', latest_version: '1.1.0',
        current_build: 1, target_build: 2, message: '发现同版本更新', changelog: 'fixture-update-notes'};
    } else if (endpoint === 'update-install' || endpoint === 'plugin-reload') {
      currentJob = 'fixture-job'; pollCount = 0;
      // 让第二次点击发生在首个提交仍未响应时，检查前端并发保护。
      await new Promise(resolve => setTimeout(resolve, 120));
      data = {ok: true, job_id: currentJob, status: 'queued', message: '已排队'};
    } else if (endpoint === 'update-status') {
      pollCount += 1;
      if (!currentJob || url.searchParams.get('job_id') !== currentJob) {
        data = {ok: false, status: 'failed', message: 'fixture-missing-job-id'};
      } else {
        const status = pollCount === 1 ? 'installing' : terminalStatus;
        data = {ok: !(statusBusinessFailure && status === 'failed'), job_id: currentJob, status,
          message: status === 'succeeded' ? 'fixture-maintenance-succeeded' : status === 'failed' ? 'fixture-maintenance-failed' : 'fixture-maintenance-running'};
      }
    }
    res.writeHead(200, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'});
    res.end(JSON.stringify({ok: data.ok !== false, data}));
    return;
  }
  const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
  if (!['index.html', 'app.js', 'style.css'].includes(name)) {res.writeHead(404); res.end(); return;}
  let body = fs.readFileSync(path.join(pageRoot, name), 'utf8');
  if (name === 'index.html') body = body.replace('<head>', '<head>' + bridge);
  res.writeHead(200, {'Content-Type': name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html', 'Cache-Control': 'no-store'});
  res.end(body);
});
const requestCount = endpoint => calls.filter(call => call.endpoint === endpoint).length;
const failures = [];
let passed = 0;

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless: true, ...(process.env.TEST_BROWSER_CHANNEL ? {channel: process.env.TEST_BROWSER_CHANNEL} : {})});
  const baseUrl = `http://127.0.0.1:${server.address().port}/`;
  async function runCase(name, test) {
    if (process.env.TEST_CASE && !name.includes(process.env.TEST_CASE)) return;
    resetFixture();
    const page = await browser.newPage({viewport: {width: 1280, height: 900}});
    page.setDefaultTimeout(10000);
    const pageErrors = [];
    page.on('pageerror', error => pageErrors.push(error.message));
    page.on('dialog', dialog => dialog.accept());
    try {
      await page.goto(baseUrl);
      await page.waitForFunction(() => document.querySelectorAll('.supplier-card').length === 2);
      await test(page);
      assert.deepEqual(pageErrors, [], '页面不应出现未处理的 JavaScript 错误');
      passed += 1;
      console.log('PASS: ' + name);
    } catch (error) {
      failures.push(name);
      console.error('FAIL: ' + name + '\n' + error.message.split('\n').slice(0, 6).join('\n'));
    } finally {await page.close();}
  }
  const suppliersTab = page => page.getByRole('tab', {name: '供应商与协议', exact: true}).click();
  const dirtyModel = async page => {
    await suppliersTab(page);
    await page.locator('#protocol-model').fill('fixture-unsaved-model');
    assert.equal(await page.locator('#dirty-flag').isVisible(), true);
  };
  async function saveConfig(page) {
    try {
      await Promise.all([
        page.waitForResponse(r => new URL(r.url()).pathname === '/_bridge/config' && r.request().method() === 'POST'),
        page.locator('#save-btn').click(),
      ]);
    } catch (error) {
      throw new Error(error.message.split('\n')[0] + ' | 页面提示：' + await page.locator('#error-banner-text').textContent() + ' | Toast：' + await page.locator('#toast-host').textContent() + ' | 请求：' + calls.map(call => call.method + ':' + call.endpoint).join(','));
    }
    await page.waitForFunction(() => !document.querySelector('#save-btn').disabled);
  }
  async function checkUpdate(page) {
    assert.equal(await page.locator('#maintenance-message').count(), 1, '缺少更新任务状态区');
    await page.locator('#update-check-btn').click();
    await page.waitForFunction(() => document.querySelector('#maintenance-message').textContent.includes('发现同版本更新'));
    assert.match(await page.locator('#maintenance-message').textContent(), /发现同版本更新/);
    assert.equal(await page.locator('#update-install-btn').isEnabled(), true, '发现同版本新构建后应允许更新');
  }
  async function clickTwice(page, id) {
    await page.locator('#' + id).evaluate(button => {button.click(); button.click();});
  }
  async function terminalMessage(page, message) {
    await page.waitForFunction(value => document.querySelector('#maintenance-message').textContent.includes(value), message, {timeout: 16000});
  }
  try {
    await runCase('选中二号数字名称后图片模型面板立即使用二号目录', async page => {
      await suppliersTab(page);
      await page.locator('.supplier-card').nth(1).locator('input[name="active-supplier"]').check();
      const modelCard = page.locator('.card').filter({has: page.locator('#protocol-model')});
      const response = page.waitForResponse(r => new URL(r.url()).pathname === '/_bridge/models');
      await modelCard.getByRole('button', {name: /获取.*模型列表/}).click();
      await response;
      const request = calls.findLast(call => call.endpoint === 'models');
      assert.equal(request.params.supplier, '22', '切换后模型目录仍请求一号供应商');
      await page.waitForFunction(() => document.querySelector('#protocol-model').closest('.card').textContent.includes('second-image-model'));
      assert.equal(await page.locator('.supplier-card').nth(1).locator('input[name="active-supplier"]').isChecked(), true);
      assert.equal(saved.active_supplier, 'First', '未保存前不得改写服务端配置');
    });
    for (const width of [1280, 390]) {
      await runCase(width + 'px toast 显示时可立即保存并在重载后保持二号', async page => {
        await page.setViewportSize({width, height: 900});
        await suppliersTab(page);
        await page.locator('.supplier-card').nth(1).locator('input[name="active-supplier"]').check();
        assert.match(await page.locator('#toast-host').textContent(), /设为当前供应商/);
        const started = Date.now();
        await saveConfig(page);
        const elapsed = Date.now() - started;
        assert.ok(elapsed < 4000, 'toast 不应遮挡保存按钮：保存耗时 ' + elapsed + 'ms');
        assert.equal(saved.active_supplier, '22');
        await page.reload();
        await page.waitForFunction(() => document.querySelectorAll('.supplier-card').length === 2);
        assert.equal(await page.locator('.supplier-card').nth(1).locator('input[name="active-supplier"]').isChecked(), true);
        assert.equal(await page.locator('#dirty-flag').isVisible(), false);
      });
    }
    await runCase('保存业务失败保留修改且不显示保存成功', async page => {
      saveFailure = true;
      await dirtyModel(page);
      await saveConfig(page);
      assert.equal(await page.locator('#dirty-flag').isVisible(), true, 'data.ok=false 后不能清掉未保存状态');
      assert.equal(await page.locator('#protocol-model').inputValue(), 'fixture-unsaved-model');
      assert.match(await page.locator('#error-banner-text').textContent(), /fixture-config-save-failed/);
      assert.equal(await page.locator('#ok-banner').isVisible(), false, '失败后不能展示保存成功');
      assert.equal(saved.protocol_models.openai.model, 'fixture-image');
    });
    await runCase('未保存修改时更新与重载均不提交', async page => {
      await checkUpdate(page);
      await dirtyModel(page);
      await clickTwice(page, 'update-install-btn');
      await clickTwice(page, 'plugin-reload-btn');
      assert.equal(requestCount('update-install'), 0);
      assert.equal(requestCount('plugin-reload'), 0);
      assert.equal(await page.locator('#dirty-flag').isVisible(), true);
    });
    await runCase('同版本更新检测到安装轮询成功且不会重复提交', async page => {
      await checkUpdate(page);
      await clickTwice(page, 'update-install-btn');
      await terminalMessage(page, 'fixture-maintenance-succeeded');
      assert.equal(requestCount('update-install'), 1, '重复点击不能创建两个更新任务');
      assert.ok(requestCount('update-status') >= 2, '应等待异步任务完成而非把排队当成功');
      assert.ok(calls.filter(call => call.endpoint === 'update-status').every(call => call.params.job_id === 'fixture-job'));
      await page.waitForFunction(() => { const button = document.querySelector('#plugin-reload-btn'); return button && !button.disabled; });
      assert.equal(await page.locator('#plugin-reload-btn').isEnabled(), true);
      if (process.env.TEST_SCREENSHOT) {
        await page.locator('#maintenance-message').scrollIntoViewIfNeeded();
        await page.screenshot({path: process.env.TEST_SCREENSHOT, type: 'jpeg', quality: 70});
      }
    });
    await runCase('更新任务失败展示错误并停止轮询', async page => {
      terminalStatus = 'failed';
      statusBusinessFailure = true;
      await checkUpdate(page);
      await clickTwice(page, 'update-install-btn');
      await terminalMessage(page, 'fixture-maintenance-failed');
      assert.equal(requestCount('update-install'), 1);
      const finishedPolls = requestCount('update-status');
      await page.waitForTimeout(1800);
      assert.equal(requestCount('update-status'), finishedPolls, '任务失败后不应继续轮询');
      await page.waitForFunction(() => { const button = document.querySelector('#plugin-reload-btn'); return button && !button.disabled; });
      assert.equal(await page.locator('#plugin-reload-btn').isEnabled(), true);
    });
    await runCase('手动插件重载等待异步成功且不会重复提交', async page => {
      assert.equal(await page.locator('#plugin-reload-btn').count(), 1, '缺少插件重载按钮');
      await clickTwice(page, 'plugin-reload-btn');
      await terminalMessage(page, 'fixture-maintenance-succeeded');
      assert.equal(requestCount('plugin-reload'), 1);
      assert.equal(requestCount('update-install'), 0);
      assert.ok(requestCount('update-status') >= 2);
    });
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
  console.log(`RESULT: ${passed} passed, ${failures.length} failed`);
  if (failures.length) process.exitCode = 1;
})().catch(error => {console.error(error); server.close(); process.exitCode = 1;});
