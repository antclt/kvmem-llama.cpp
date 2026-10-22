// Exercise the built chat page against both native /props contracts, without a model.
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const directory=path.resolve(process.env.UI_DIST||path.join(__dirname,'../ui-dist'));
const types={'.html':'text/html','.js':'text/javascript','.css':'text/css','.json':'application/json','.svg':'image/svg+xml','.png':'image/png','.ico':'image/x-icon','.woff2':'font/woff2'};
const server=http.createServer((request,response)=>{
 const filename=path.resolve(directory,'.'+decodeURIComponent(new URL(request.url,'http://localhost').pathname));
 const target=filename===directory?path.join(directory,'index.html'):filename;
 if(!target.startsWith(directory+path.sep)||!fs.existsSync(target)||!fs.statSync(target).isFile()){response.writeHead(404);return response.end();}
 response.setHeader('Content-Type',types[path.extname(target)]||'application/octet-stream');fs.createReadStream(target).pipe(response);
});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser=await chromium.launch({headless:true,...(process.env.CHROMIUM?{executablePath:process.env.CHROMIUM}:{})});
 try{
  for(const backend of ['llamacpp','ninfer']){
   const context=await browser.newContext(),page=await context.newPage(),errors=[],bad=[],sent=[];
   page.on('pageerror',e=>errors.push(e.message));page.on('response',r=>{if(r.status()>=400)bad.push(r.url());});
   const props={default_generation_settings:{n_ctx:8192,params:{max_tokens:64,temperature:.2}},modalities:{vision:false}};
   if(backend==='ninfer')Object.assign(props,{model_alias:'native-ninfer',kvmem:{enabled:true,selected_tokens:768}});
   else Object.assign(props,{model_name:'native-llama',kvmem:{generation_limit:8192,defaults:{enable_thinking:false,reasoning_budget_tokens:0,chat_template_kwargs:{}},sampling:{non_thinking:{temperature:.7},thinking:{temperature:.6}}}});
   await page.route('**/props',route=>route.fulfill({json:props}));
   await page.route('**/v1/chat/completions',route=>{
    sent.push(route.request().postDataJSON());
    return route.fulfill({contentType:'text/event-stream',body:'data: '+JSON.stringify({choices:[{delta:{content:'Fixture reply'}}]})+'\n\ndata: [DONE]\n\n'});
   });
   try{
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.getByText(backend==='ninfer'?'native-ninfer':'native-llama',{exact:true}).waitFor();
    await page.getByRole('button',{name:'参数',exact:true}).click();
    assert.equal(await page.getByLabel('temperature',{exact:true}).getAttribute('placeholder'),backend==='ninfer'?'0.2':'0.7');
    const thinking=page.getByRole('region',{name:'生成参数'}).locator('select');
    assert.equal(await thinking.locator('option:checked').textContent(),`跟随服务器（${backend==='ninfer'?'未报告':'关闭'}）`);
    await page.getByLabel('最大输出',{exact:true}).fill('16');
    await page.getByLabel('消息输入',{exact:true}).fill('Hello');
    await page.getByRole('button',{name:'发送',exact:true}).click();
    await page.getByText('Fixture reply',{exact:true}).waitFor();
    assert.equal(sent.length,1);assert.equal(sent[0].model,backend==='ninfer'?'native-ninfer':'native-llama');
    assert.equal(sent[0].max_tokens,16);assert.equal(Object.hasOwn(sent[0],'chat_template_kwargs'),false,'unknown server defaults must remain server-owned');
    await thinking.selectOption('on');
    assert.equal(await page.getByLabel('temperature',{exact:true}).getAttribute('placeholder'),backend==='ninfer'?'':'0.6');
    await page.getByLabel('最大输出',{exact:true}).fill('8193');
    await page.getByLabel('消息输入',{exact:true}).fill('Over the bound');
    await page.getByRole('button',{name:'发送',exact:true}).click();
    await page.getByRole('alert').filter({hasText:'1..8192'}).waitFor();assert.equal(sent.length,1);
    assert.deepEqual(errors,[]);assert.deepEqual(bad,[]);
    console.log('PASS built chat props:',backend);
   }finally{await context.close();}
  }
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>server.close());
