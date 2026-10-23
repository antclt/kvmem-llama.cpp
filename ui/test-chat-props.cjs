// Built unified chat with the real adapter for three native props contracts; no GPU.
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {spawnSync}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const directory=path.resolve(process.env.UI_DIST||path.join(__dirname,'../build/share/kvmem/ui'));
assert.ok(fs.existsSync(path.join(directory,'index.html')),'Built chat index is missing: '+directory);
const types={'.html':'text/html','.js':'text/javascript','.css':'text/css','.json':'application/json','.svg':'image/svg+xml','.png':'image/png','.ico':'image/x-icon','.woff2':'font/woff2'};
const server=http.createServer((request,response)=>{
 const filename=path.resolve(directory,'.'+decodeURIComponent(new URL(request.url,'http://localhost').pathname));
 const target=filename===directory?path.join(directory,'index.html'):filename;
 if(!target.startsWith(directory+path.sep)||!fs.existsSync(target)||!fs.statSync(target).isFile()){response.writeHead(404);return response.end();}
 response.setHeader('Content-Type',types[path.extname(target)]||'application/octet-stream');fs.createReadStream(target).pipe(response);
});
function settings(backend){
 const props={model_alias:'native-'+backend,default_generation_settings:{n_ctx:8192,params:{n_predict:64,temperature:.2}},modalities:{vision:false}};
 if(backend==='ninfer')props.kvmem={enabled:true,selected_tokens:768};
 if(backend==='llamacpp')props.kvmem={generation_limit:8192,defaults:{enable_thinking:false,reasoning_budget_tokens:0,chat_template_kwargs:{}},sampling:{non_thinking:{temperature:.7},thinking:{temperature:.6}}};
 const input={state:{state:'ready',instance:'fixture-'+backend,url:'http://localhost:99',active_config:{parameters:{Backend:backend}}},props,
  settings:backend==='polystrata'?{effective:{reasoning_effort:'none',max_tokens:64,reasoning_budget_tokens:32,preserve_thinking:true}}:null};
 const script='import json,sys;sys.path.insert(0,"scripts");from console_chat import context_payload;print(json.dumps(context_payload(**json.load(sys.stdin))))';
 const result=spawnSync(process.env.PYTHON||'python',['-X','utf8','-c',script],{cwd:path.join(__dirname,'..'),input:JSON.stringify(input),encoding:'utf8'});
 assert.equal(result.status,0,result.stderr);return JSON.parse(result.stdout);
}
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser=await chromium.launch({headless:true,...(process.env.CHROMIUM?{executablePath:process.env.CHROMIUM}:{})});
 try{
  for(const backend of ['llamacpp','ninfer','polystrata']){
   const context=await browser.newContext(),page=await context.newPage(),errors=[],bad=[],sent=[];
   const ctx=settings(backend);let timings={predicted_n:1,predicted_ms:.001,predicted_per_second:1000000};
   page.on('pageerror',e=>errors.push(e.message));page.on('response',r=>{if(r.status()>=400)bad.push(r.url());});
   await page.route('**/api/launcher/info',route=>route.fulfill({json:{token:'fixture-token'}}));
   await page.route('**/api/chat/context',route=>route.fulfill({json:ctx}));
   await page.route('**/api/chat/requests/*',route=>route.fulfill({json:{state:'completed'}}));
   await page.route('**/api/chat/cancel',route=>route.fulfill({json:{cancelled:true}}));
   await page.route('**/api/chat/completions',route=>{
    sent.push(route.request().postDataJSON());
    assert.equal(route.request().headers()['x-kvmem-token'],'fixture-token');
    assert.equal(route.request().headers()['x-kvmem-instance'],ctx.instance);
    return route.fulfill({contentType:'text/event-stream',body:'data: '+JSON.stringify({choices:[{delta:{content:'Fixture reply'},finish_reason:'stop'}],timings})+'\n\ndata: [DONE]\n\n'});
   });
   try{
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.getByText(ctx.model,{exact:true}).waitFor();
    await page.getByRole('button',{name:'参数',exact:true}).click();
    const dialog=page.getByRole('dialog',{name:'生成参数'}),thinking=dialog.getByLabel('思考',{exact:true});
    assert.equal(await dialog.getByLabel('temperature',{exact:true}).inputValue(),backend==='llamacpp'?'0.7':'0.2');
    assert.equal(await thinking.inputValue(),backend==='ninfer'?'unknown':'false');
    const output=dialog.getByLabel('总输出上限 · tokens',{exact:true});
    await output.fill('16');await output.press('Tab');
    await output.fill('8193');await output.press('Tab');
    await dialog.getByRole('alert').filter({hasText:'max_tokens'}).waitFor();
    assert.equal(await output.inputValue(),'16','invalid value must not look accepted');
    await dialog.getByLabel('关闭面板').click();
    const send=async()=>{
     const count=await page.locator('.message-footer').count();
     await page.getByLabel('消息输入',{exact:true}).fill('Hello');
     await page.getByRole('button',{name:/^发送/}).click();
     await page.locator('.message-footer').nth(count).waitFor();
     await page.getByRole('button',{name:'参数',exact:true}).waitFor();
     return page.locator('.message-footer').last();
    };
    let footer=await send();
    assert.equal(sent[0].model,ctx.model);assert.equal(sent[0].max_tokens,16);
    assert.equal(Object.hasOwn(sent[0],'chat_template_kwargs'),false,'untouched defaults stay server-owned');
    assert.equal(await footer.getByText('1 token',{exact:true}).count(),1);
    assert.equal(await footer.getByText(/tok\/s/).count(),0,'one token has no decode-step rate');
    for(const [native,expected] of [
     [{predicted_n:5,predicted_ms:.001,predicted_per_second:12.5},'平均 12.5 tok/s'],
     [{predicted_n:5,predicted_ms:.001,predicted_per_second:0},null],
     [{predicted_n:5,predicted_ms:.001},null],
     [{predicted_n:5,predicted_ms:.001,predicted_per_second:-1},null],
    ]){
     timings=native;footer=await send();
     assert.equal(await footer.getByText('5 token',{exact:true}).count(),1);
     if(expected)assert.equal(await footer.getByText(expected,{exact:true}).count(),1);
     else assert.equal(await footer.getByText(/tok\/s/).count(),0,'do not fabricate rate from elapsed time');
    }
    await page.getByRole('button',{name:'参数',exact:true}).click();
    await thinking.selectOption('true');
    assert.equal(await dialog.getByLabel('temperature',{exact:true}).inputValue(),backend==='llamacpp'?'0.6':'0.2');
    await dialog.getByLabel('关闭面板').click();await send();
    assert.equal(sent.at(-1).chat_template_kwargs.enable_thinking,true);
    if(backend==='polystrata')assert.equal(sent.at(-1).reasoning_effort,'high');
    await page.reload();await page.locator('.message-footer').nth(5).waitFor();
    assert.deepEqual(errors,[]);assert.deepEqual(bad,[]);
    console.log('PASS unified chat settings, means, thinking and history:',backend);
   }finally{await context.close();}
  }
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>server.close());
