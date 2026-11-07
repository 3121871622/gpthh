/** 完整页面回归：安装 Playwright 与浏览器后运行 node tests/test_video_browser.cjs。 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const pageRoot = path.join(root, 'pages/settings');
const config = JSON.parse(execFileSync(process.env.TEST_PYTHON || 'python', ['-B', '-c',
  'import json; from core.config import PluginConfig; print(json.dumps(PluginConfig({}).to_public_dict()))'], {cwd: root, encoding: 'utf8'}));
const version = fs.readFileSync(path.join(root, 'VERSION'), 'utf8').trim();
const supplierNames = ['Image provider 1', 'Image provider 2', 'Image provider 3', 'Image provider 4', 'Video provider'];
config.suppliers = supplierNames.map((name, index) => ({name, base_url: 'https://provider-' + index + '.example.invalid', api_key_set: true, api_key_masked: 'configured'}));
config.active_supplier = supplierNames[0];
config.active_protocol = 'openai';
config.protocol_models.openai = {model: 'fixture-image-model', edit_model: 'fixture-edit-model'};
config.video_supplier = '';
config.video_model = '';
let saved = structuredClone(config);
const calls = [], browserErrors = [], persisted = [];
const bridge = `<script>window.AstrBotPluginPage={ready:async()=>({}),onContext:()=>{},apiGet:async(p,a)=>fetch('/_bridge/'+p+'?'+new URLSearchParams(a||{})).then(r=>r.json()),apiPost:async(p,b)=>fetch('/_bridge/'+p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json())};</script>`;
const server = http.createServer(async(req,res) => {
  const url = new URL(req.url,'http://local');
  if(url.pathname.startsWith('/_bridge/')) {
    const endpoint = url.pathname.slice(9);
    calls.push({endpoint,method:req.method,params:Object.fromEntries(url.searchParams)});
    if(req.method === 'POST') {
      let body = ''; for await (const chunk of req) body += chunk;
      if(endpoint === 'config') {
        const payload = JSON.parse(body); persisted.push(payload); saved = {...saved,...payload};
      }
    }
    const data = endpoint === 'config' ? saved : endpoint === 'suppliers' ? {suppliers:saved.suppliers} : endpoint === 'status' ? {version} : endpoint === 'models' ? {ok:true, models:['fixture-image-model','fixture-video-model','fixture-other-model'],total:3} : {};
    res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({ok:true,data}));return;
  }
  const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
  if(!['index.html','app.js','style.css'].includes(name)) {res.writeHead(404);res.end();return;}
  let body = fs.readFileSync(path.join(pageRoot,name));
  if(name === 'index.html') body = Buffer.from(body.toString().replace('<head>', '<head>'+bridge));
  res.writeHead(200,{'Content-Type':name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html','Cache-Control':'no-store'});res.end(body);
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser = await chromium.launch({headless:true,...(process.env.TEST_BROWSER_CHANNEL?{channel:process.env.TEST_BROWSER_CHANNEL}:{})});
  try {
    const page = await browser.newPage({viewport:{width:1280,height:900}});
    page.on('pageerror', error=>browserErrors.push(error.message));
    await page.goto('http://127.0.0.1:'+server.address().port+'/');
    await page.locator('#video-model').waitFor({state:'attached'});
    await page.getByRole('tab',{name:'视频生成',exact:true}).click();
    assert.equal(await page.locator('#video-model').isVisible(),true);
    const firstModelRect = await page.locator('#video-model').boundingBox();
    assert.ok(firstModelRect.y < 900,'视频模型不应藏在首屏之外');
    for(const id of ['video-model','video-supplier','video-api-mode','video-enabled','video-poll-interval','video-progress-interval','video-duration-timeout','video-timeout','video-max-concurrent']) {
      assert.equal(await page.locator('#'+id).count(),1,id+' 必须唯一');
      assert.equal(await page.locator('#'+id).isVisible(),true,id+' 必须可见');
    }
    await page.locator('#video-supplier').selectOption('Video provider');
    await page.locator('#video-model-fetch').click();
    await page.locator('#video-model-search').waitFor({state:'visible'});
    await page.locator('#video-model-search').fill('fixture-video');
    await page.getByRole('group',{name:'视频模型候选'}).getByRole('button').click();
    assert.equal(await page.locator('#video-model').inputValue(),'fixture-video-model');
    const query = calls.findLast(item=>item.endpoint === 'models');
    assert.deepEqual(query.params,{supplier:'Video provider',protocol:'openai',include_all:'1',refresh:'1'});
    await page.locator('#video-progress-interval').fill('15');
    await page.locator('#video-duration-timeout').fill('180');
    await page.locator('#save-btn').click();
    await page.waitForFunction(()=>document.querySelector('#ok-banner-text').textContent.includes('已保存'));
    assert.equal(persisted.at(-1).video_supplier,'Video provider');
    assert.equal(persisted.at(-1).video_model,'fixture-video-model');
    assert.equal(persisted.at(-1).video_progress_interval,15);
    assert.equal(persisted.at(-1).video_duration_timeout,180);
    assert.equal(persisted.at(-1).active_supplier,'Image provider 1');
    assert.equal(persisted.at(-1).protocol_models.openai.model,'fixture-image-model');
    await page.reload();
    await page.locator('#video-model').waitFor({state:'attached'});
    assert.equal(await page.locator('#video-model').inputValue(),'fixture-video-model');
    assert.equal(await page.locator('#video-supplier').inputValue(),'Video provider');
    // 模型列表之外的合法型号也应允许手工填写并保存。
    await page.locator('#video-model').fill('manual-video-id');
    await page.locator('#save-btn').click();
    await page.waitForFunction(()=>document.querySelector('#ok-banner-text').textContent.includes('已保存'));
    assert.equal(persisted.at(-1).video_model,'manual-video-id');
    await page.getByRole('tab',{name:'模型与尺寸',exact:true}).click();
    await page.getByRole('link',{name:'前往视频生成设置',exact:true}).click();
    assert.equal(await page.locator('#section-videos').getAttribute('hidden'),null);
    await page.getByRole('link',{name:'设置开始、进度与完成文案',exact:true}).click();
    assert.equal(await page.locator('#section-prompts').getAttribute('hidden'),null);
    await page.getByRole('tab',{name:'视频生成',exact:true}).click();
    if(process.env.UI_SCREENSHOT) {
      await page.locator('#section-videos').evaluate(async node=>{
        await Promise.all(node.getAnimations({subtree:true}).map(animation=>animation.finished.catch(()=>{})));
        window.scrollTo({top:0,behavior:'instant'});
      });
      await page.screenshot({path:process.env.UI_SCREENSHOT});
    }
    await page.setViewportSize({width:390,height:844});
    await page.getByRole('tab',{name:'供应商与协议',exact:true}).click();
    await page.getByRole('tab',{name:'视频生成',exact:true}).click();
    assert.equal(await page.locator('#video-model').isVisible(),true);
    assert.equal(await page.locator('#video-model').inputValue(),'manual-video-id');
    assert.deepEqual(browserErrors,[]);
    console.log(JSON.stringify({passed:true,version,desktop:'1280x900',mobile:'390x844',videoModelTop:firstModelRect.y,saveRoundTrips:persisted.length,independentSupplier:true,errors:browserErrors}));
  }finally {await browser.close();server.close();}
})().catch(error=>{console.error(error.stack);server.close();process.exitCode=1;});
