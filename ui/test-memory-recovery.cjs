/* Real console endpoints and browser UI, simulated worker: no GPU or model required. */
const assert=require('node:assert/strict'),path=require('node:path'),fs=require('node:fs'),os=require('node:os');
const {spawn}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),state=fs.mkdtempSync(path.join(os.tmpdir(),'memory-ui-'));
const port=Number(process.env.CONSOLE_TEST_PORT||18469),base=`http://127.0.0.1:${port}`;
const child=spawn(process.env.PYTHON||'python',['-B',path.join(__dirname,'memory-fixture.py'),String(port),state],{windowsHide:true});
let browser,stderr='';child.stderr.on('data',x=>stderr+=x);
async function until(fn){for(let i=0;i<100;i++){try{if(await fn())return;}catch{}await new Promise(r=>setTimeout(r,100));}throw Error(stderr||'timeout');}
async function checkRanking(page){
 const failed=await (await fetch(base+'/api/launcher/status')).json();
 const info=await (await fetch(base+'/api/launcher/info')).json(),persisted=info.draft;
 let status;
 // Ranking changes only the status fixture. Reusing its configuration snapshot
 // avoids a live fetch surviving page reload or masking an assertion at teardown.
 const serveInfo=route=>route.fulfill({json:{...info,status}});
 const serveStatus=route=>route.fulfill({json:status});
 await page.route('**/api/launcher/info',serveInfo);
 await page.route('**/api/launcher/status',serveStatus);
 try{
   const cases=[
     {name:'all above target',budgets:[37184,66816,49280],order:['int8','rk8v4','nvfp4'],reason:'highest-precision'},
     {name:'INT8 exactly 32K',budgets:[32768,65536,49152],order:['int8','rk8v4','nvfp4'],reason:'highest-precision'},
     {name:'RK8V4 exactly 32K, INT8 one page below',budgets:[32704,65536,32768],order:['rk8v4','nvfp4','int8'],reason:'highest-precision'},
     {name:'only NVFP4 reaches target',budgets:[32704,32768,32000],order:['nvfp4','int8','rk8v4'],reason:'highest-precision'},
     {name:'all below, RK8V4 largest',budgets:[28672,30016,31744],order:['rk8v4','nvfp4','int8'],reason:'largest budget'},
     {name:'all below, NVFP4 largest',budgets:[20032,30016,25024],order:['nvfp4','rk8v4','int8'],reason:'largest budget'},
     {name:'equal budgets prefer precision',budgets:[32000,32000,32000],order:['int8','rk8v4','nvfp4'],reason:'largest budget'},
     {name:'unavailable INT8 cannot win',budgets:[null,65536,32768],order:['rk8v4','nvfp4','int8'],reason:'highest-precision'},
     {name:'only one available below target',budgets:[null,30016,null],order:['nvfp4','int8','rk8v4'],reason:'largest budget'},
     {name:'no available option',budgets:[null,null,null],order:['int8','rk8v4','nvfp4'],reason:null}
   ];
   for(const test of cases){
     status=structuredClone(failed);
     status.memory_diagnosis.options.forEach((option,index)=>{
       const budget=test.budgets[index];option.available=budget!==null;
       option.changes=budget===null?{}:{...option.changes,Budget:budget};
       if(budget===null)option.reason='minimum_does_not_fit';
     });
     await page.reload();await page.locator('[data-memory-option]').first().waitFor();
     assert.deepEqual(await page.locator('[data-memory-option]').evaluateAll(xs=>xs.map(x=>x.dataset.memoryOption)),test.order,test.name);
     const recommended=page.locator('[data-memory-recommended=true]');
     assert.equal(await recommended.count(),test.reason?1:0,test.name);
     if(test.reason){
       assert.equal(await recommended.getAttribute('data-memory-option'),test.order[0],test.name);
       assert.equal(await recommended.locator('.l-memory-badge').innerText(),'Recommended');
       assert.ok((await recommended.locator('.l-memory-reason').innerText()).includes(test.reason),test.name);
     }
     assert.equal(await page.locator('[data-memory-retry]').count(),test.budgets.filter(x=>x!==null).length,test.name);
   }
 }finally{
   await page.unrouteAll({behavior:'wait'});
 }
 assert.deepEqual(await (await fetch(base+'/api/launcher/status')).json(),failed,'Ranking must not change the failed instance or launch parameters');
 assert.deepEqual((await (await fetch(base+'/api/launcher/info')).json()).draft,persisted,'Ranking must not save new configuration');
 await page.reload();await page.locator('[data-memory-recommended=true]').waitFor();
 assert.equal(await page.locator('[data-memory-option]').first().getAttribute('data-memory-option'),'nvfp4');
}
(async()=>{
 await until(async()=>(await fetch(base+'/api/launcher/status')).ok);
 browser=await chromium.launch({headless:true,...(process.env.CHROMIUM?{executablePath:process.env.CHROMIUM}:{})});
 const page=await browser.newPage({viewport:{width:1440,height:1100}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base+'/monitor/?lang=en#config');await page.locator('[data-param=Model]').waitFor();
 for(const dtype of ['int8','nvfp4','rk8v4']){
   await page.locator('[data-param=Model]').fill('test.ninfer');
   await page.locator('[data-gpu-choice]').first().check();
   await page.locator('[data-param=Budget]').fill('65536');await page.locator('[data-param=Reserve]').fill('8192');
   await page.locator('[data-action=start]').click();
   await page.getByText('Insufficient GPU memory at startup',{exact:true}).waitFor({timeout:10000}).catch(async e=>{
     console.error('STATUS',await (await fetch(base+'/api/launcher/status')).text());
     console.error('MESSAGE',await page.locator('[data-launch-message]').innerText());
     console.error('INVALID',await page.locator('form :invalid').evaluateAll(xs=>xs.map(x=>({name:x.dataset.param,value:x.value,message:x.validationMessage}))));
     console.error('FIXTURE',stderr);throw e;
   });
   assert.equal(await page.locator('[data-memory-option]').count(),3);
   assert.match(await page.locator('[data-memory-option=int8]').innerText(),/8,192 → 4,096/);
   if(dtype==='int8'){
     await checkRanking(page);
     const shots=process.env.MEMORY_SCREENSHOTS;
     if(shots){fs.mkdirSync(shots,{recursive:true});await page.screenshot({path:path.join(shots,'desktop.png'),fullPage:true});}
     await page.locator('[data-param=Budget]').fill('60000');
     await page.locator('[data-memory-retry=int8]').click();
     await until(async()=>(await page.locator('[data-dialog=error]').innerText()).includes('Settings changed'));
     assert.equal((await (await fetch(base+'/api/launcher/status')).json()).state,'exited');
     await page.keyboard.press('Escape');
     await page.locator('[data-param=Budget]').fill('65536');
     await page.locator('[data-language-select]').selectOption('zh-CN');
     await page.setViewportSize({width:390,height:844});
     assert.equal(await page.locator('[data-memory-recommended=true] .l-memory-badge').innerText(),'推荐');
     assert.match(await page.locator('.l-memory-reason').innerText(),/均低于 32K/);
     await page.locator('[data-memory-retry=int8]').scrollIntoViewIfNeeded();
     assert.ok(await page.locator('[data-memory-retry=int8]').isVisible());
     assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
     if(shots)await page.screenshot({path:path.join(shots,'mobile.png'),fullPage:true});
     await page.setViewportSize({width:1440,height:1100});await page.locator('[data-language-select]').selectOption('en');
   }
   await page.locator(`[data-memory-retry=${dtype}]`).click();
   await until(async()=>(await page.locator('[data-memory-recovery]').innerText()).includes('passed startup validation'));
   assert.equal(await page.locator('[data-param=Reserve]').inputValue(),'4096');
   assert.equal(await page.locator('[data-param=KvType]').inputValue(),dtype);
   assert.ok(Number(await page.locator('[data-param=Budget]').inputValue())<65536);
   await page.locator('[data-action=stop]').click();
   await until(async()=>!(await page.locator('[data-param=Budget]').isDisabled()));
 }
 assert.deepEqual(errors,[]);
 console.log('PASS: 10 recommendation ranking cases (32K boundary, precision, largest budget, ties, unavailable), no automatic configuration changes; INT8/NVFP4/RK8V4 retry, 4K reserve, stale draft rejection, startup verification, Chinese/English, desktop/mobile');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();child.kill();});
