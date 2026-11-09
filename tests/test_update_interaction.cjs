/** 真实 Edge 的检查更新交互回归；全部 bridge 请求由本页 fixture 接管，不请求供应商。 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const pageRoot = process.env.TEST_PAGE_ROOT || path.join(root, 'pages/settings');
const config = JSON.parse(execFileSync(process.env.TEST_PYTHON || 'python', ['-B', '-c',
  'import json; from core.config import PluginConfig; print(json.dumps(PluginConfig({}).to_public_dict()))'],
  {cwd: root, encoding: 'utf8'}));
config.suppliers = [{name: 'Fixture', base_url: 'https://fixture.invalid/v1', api_key_set: true}];
config.active_supplier = 'Fixture';

const bridge = `<script>
const fixtureConfig=${JSON.stringify(config)};
window.updateFixture={checks:[],polls:[],posts:[],jobId:'fixture-maintenance-job'};
window.AstrBotPluginPage={ready:async()=>({}),onContext:()=>{},apiGet:async(endpoint)=>{
 const fixture=window.updateFixture;
 if(endpoint==='config')return structuredClone(fixtureConfig);
 if(endpoint==='suppliers')return {suppliers:fixtureConfig.suppliers};
 if(endpoint==='status')return {version:'1.1.0',build:2026111003};
 if(endpoint==='update-check')return new Promise((resolve,reject)=>fixture.checks.push({resolve,reject}));
 if(endpoint==='update-status')return new Promise((resolve,reject)=>fixture.polls.push({resolve,reject}));
 return {};
},apiPost:async(endpoint,body)=>{const fixture=window.updateFixture;fixture.posts.push({endpoint,body});return {ok:true,job_id:fixture.jobId,status:'queued',message:'fixture-queued'};}};
</script>`;

const server = http.createServer((req, res) => {
  const name = new URL(req.url, 'http://fixture').pathname.slice(1) || 'index.html';
  if (!['index.html', 'app.js', 'style.css'].includes(name)) {res.writeHead(404);res.end();return;}
  let text = fs.readFileSync(path.join(pageRoot, name), 'utf8');
  if (name === 'index.html') text = text.replace('<head>', '<head>' + bridge);
  res.writeHead(200, {'Content-Type': name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html', 'Cache-Control': 'no-store'});
  res.end(text);
});

const result = (message = 'fixture-check-current', target = 2) => ({ok: true, update_available: true, current_version: '1.1.0', latest_version: '1.1.0', current_build: 1, target_build: target, message, changelog: message + '-notes'});
let passed = 0;
const failures = [];

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = 'http://127.0.0.1:' + server.address().port;
  const browser = await chromium.launch({headless: true, channel: process.env.TEST_BROWSER_CHANNEL || 'msedge'});
  console.log('BROWSER: ' + browser.version());
  async function runCase(name, test, media = 'no-preference') {
    if (process.env.TEST_CASE && !name.includes(process.env.TEST_CASE)) return;
    const page = await browser.newPage({viewport: {width: 1280, height: 800}});
    page.setDefaultTimeout(8000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    try {
      await page.emulateMedia({reducedMotion: media});
      await page.goto(origin);
      await page.waitForFunction(() => document.querySelectorAll('.supplier-card').length === 1);
      await test(page);
      assert.deepEqual(errors, [], '不应出现未处理的迟到异常');
      passed += 1;
      console.log('PASS: ' + name);
    } catch (error) {
      failures.push(name);
      console.error('FAIL: ' + name + '\n' + error.message.split('\n').slice(0, 8).join('\n'));
    } finally {await page.close();}
  }
  async function beginCheck(page, count = 1) {
    await page.locator('#update-check-btn').click();
    await page.waitForFunction(expected => window.updateFixture.checks.length === expected, count);
  }
  async function resolveCheck(page, index, value) {
    await page.evaluate(({index, value}) => window.updateFixture.checks[index].resolve(value), {index, value});
    await page.waitForFunction(() => !document.querySelector('#update-check-btn').disabled);
  }
  async function assertRestored(page) {
    const button = page.locator('#update-check-btn');
    assert.equal(await button.isEnabled(), true);
    assert.equal(await button.textContent(), '检查更新');
    assert.equal(await button.getAttribute('aria-busy'), null);
    assert.equal(await button.evaluate(node => node.classList.contains('busy')), false);
    assert.equal(await page.locator('#update-check-progress').isVisible(), false);
  }
  try {
    await runCase('正常动效下独立指示器持续旋转且等待秒数更新', async page => {
      await beginCheck(page);
      assert.equal(await page.locator('#update-check-btn').getAttribute('aria-busy'), 'true');
      const indicator = page.locator('.maintenance-check-indicator');
      assert.equal(await indicator.isVisible(), true);
      const before = await indicator.evaluate(node => ({transform: getComputedStyle(node).transform, name: getComputedStyle(node).animationName, duration: getComputedStyle(node).animationDuration, count: getComputedStyle(node).animationIterationCount}));
      await page.waitForTimeout(170);
      const after = await indicator.evaluate(node => getComputedStyle(node).transform);
      assert.equal(before.name, 'maintenance-check-spin');
      assert.equal(before.count, 'infinite');
      assert.notEqual(before.transform, after, '独立指示器必须真正旋转');
      await page.waitForFunction(() => document.querySelector('#update-check-elapsed').textContent.includes('已等待 1 秒'));
      if (process.env.TEST_SCREENSHOT) await page.screenshot({path: process.env.TEST_SCREENSHOT, type: 'jpeg', quality: 65});
      await resolveCheck(page, 0, result());
      await assertRestored(page);
      assert.equal(await page.locator('#update-install-btn').isEnabled(), true);
    });
    await runCase('减少动态效果保留静态提示与秒数并可停止等待', async page => {
      await beginCheck(page);
      assert.equal(await page.locator('.maintenance-check-indicator').evaluate(node => getComputedStyle(node).animationName), 'none');
      await page.waitForFunction(() => document.querySelector('#update-check-elapsed').textContent.includes('已等待 1 秒'));
      await page.locator('#update-check-cancel-btn').click();
      await page.waitForFunction(() => !document.querySelector('#update-check-btn').disabled);
      await assertRestored(page);
      assert.match(await page.locator('#maintenance-message').textContent(), /已停止本次检查等待/);
    }, 'reduce');
    await runCase('取消后重查时迟到成功不覆盖较新的检查结果', async page => {
      await beginCheck(page);
      await page.locator('#update-check-cancel-btn').click();
      await beginCheck(page, 2);
      await resolveCheck(page, 1, result('fixture-new-result', 3));
      await page.evaluate(value => window.updateFixture.checks[0].resolve(value), result('fixture-stale-result', 999));
      await page.waitForTimeout(100);
      assert.match(await page.locator('#maintenance-message').textContent(), /fixture-new-result/);
      assert.doesNotMatch(await page.locator('#maintenance-message').textContent(), /fixture-stale-result|999/);
      assert.equal(await page.locator('#maintenance-changelog').textContent(), 'fixture-new-result-notes');
      await assertRestored(page);
    });
    await runCase('迟到失败不会解除后续请求的忙碌状态或覆盖提示', async page => {
      await beginCheck(page);
      await page.locator('#update-check-cancel-btn').click();
      await beginCheck(page, 2);
      await page.evaluate(() => window.updateFixture.checks[0].reject(new Error('fixture-stale-error')));
      await page.waitForTimeout(100);
      assert.equal(await page.locator('#update-check-btn').isDisabled(), true);
      assert.match(await page.locator('#maintenance-message').textContent(), /正在检查/);
      assert.doesNotMatch(await page.locator('#maintenance-message').textContent(), /fixture-stale-error/);
      await resolveCheck(page, 1, result());
      await assertRestored(page);
    });
    await runCase('桥接无响应20秒后自动恢复并可重新检查', async page => {
      const start = Date.now();
      await beginCheck(page);
      await page.waitForFunction(() => !document.querySelector('#update-check-btn').disabled, null, {timeout: 26000});
      assert.ok(Date.now() - start >= 19500, '应覆盖真实20秒期限，不使用缩短的测试时钟');
      await assertRestored(page);
      assert.match(await page.locator('#maintenance-message').textContent(), /超过 20 秒/);
      await beginCheck(page, 2);
      await resolveCheck(page, 1, result('fixture-after-timeout'));
      await page.evaluate(value => window.updateFixture.checks[0].resolve(value), result('fixture-timeout-stale'));
      await page.waitForTimeout(60);
      assert.match(await page.locator('#maintenance-message').textContent(), /fixture-after-timeout/);
    });
    await runCase('网络失败与业务失败均恢复按钮且不残留动画', async page => {
      await beginCheck(page);
      await page.evaluate(() => window.updateFixture.checks[0].reject(new Error('fixture-network-error')));
      await page.waitForFunction(() => !document.querySelector('#update-check-btn').disabled);
      await assertRestored(page);
      assert.match(await page.locator('#maintenance-message').textContent(), /fixture-network-error/);
      await beginCheck(page, 2);
      await resolveCheck(page, 1, {ok: false, message: 'fixture-business-error'});
      await assertRestored(page);
      assert.match(await page.locator('#maintenance-message').textContent(), /fixture-business-error/);
      assert.equal(await page.locator('#update-install-btn').isVisible(), false);
    });
    await runCase('重复点击不产生并发检查且恢复后保留宿主能力限制', async page => {
      await page.locator('#update-check-btn').evaluate(button => {button.click(); button.click();});
      assert.equal(await page.evaluate(() => window.updateFixture.checks.length), 1);
      await resolveCheck(page, 0, {...result(), install_supported: false, reload_supported: false});
      await assertRestored(page);
      assert.equal(await page.locator('#update-install-btn').isDisabled(), true);
      assert.equal(await page.locator('#plugin-reload-btn').isDisabled(), true);
    });
    await runCase('安装期间状态查询超时仅继续轮询且不重复提交或解锁', async page => {
      await beginCheck(page);
      await resolveCheck(page, 0, result());
      await page.locator('#update-install-btn').click();
      await page.waitForFunction(() => window.updateFixture.polls.length === 1);
      await page.waitForFunction(() => document.querySelector('#maintenance-message').textContent.includes('暂时失去连接'), null, {timeout: 16000});
      assert.equal(await page.locator('#update-check-btn').isDisabled(), true);
      assert.equal(await page.locator('#update-check-progress').isVisible(), false);
      assert.equal(await page.evaluate(() => window.updateFixture.posts.length), 1);
      await page.waitForFunction(() => window.updateFixture.polls.length === 2);
      await page.evaluate(() => window.updateFixture.polls[1].resolve({ok: true, job_id: window.updateFixture.jobId, status: 'succeeded', message: 'fixture-job-completed'}));
      await page.waitForFunction(() => !document.querySelector('#plugin-reload-btn').disabled);
      await page.evaluate(() => window.updateFixture.polls[0].resolve({ok: false, job_id: window.updateFixture.jobId, status: 'failed', message: 'fixture-old-poll-error'}));
      await page.waitForTimeout(100);
      assert.match(await page.locator('#maintenance-message').textContent(), /fixture-job-completed/);
      assert.equal(await page.evaluate(() => window.updateFixture.posts.length), 1);
    });
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
  console.log(`RESULT: ${passed} passed, ${failures.length} failed`);
  if (failures.length) process.exitCode = 1;
})().catch(error => {console.error(error); server.close(); process.exitCode = 1;});
