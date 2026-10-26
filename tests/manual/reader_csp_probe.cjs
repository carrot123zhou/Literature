const {chromium}=require(process.env.PW_PACKAGE_PATH);
// CSP 对照：同一 dist，一个带 Tauri CSP 头、一个不带；两边都点「启动」，对比 iframe 内核是否起来。
// argv: [plainUrl, cspUrl, pid, resultPath]
// argv: [plainBase, cspBase, pid, resultPath] → process.argv[2..5]
const PLAIN=`${process.argv[2]}/#/p/${process.argv[4]}/reader`;
const CSP=`${process.argv[3]}/#/p/${process.argv[4]}/reader`;
const RESULT=process.argv[5];

async function runOne(browser,url,label){
  const ctx=await browser.newContext({viewport:{width:1440,height:920}});
  const page=await ctx.newPage();page.setDefaultTimeout(20000);
  const console_msgs=[];const violations=[];const pageerrors=[];
  page.on('console',m=>{const t=m.text();
    if(/Content Security Policy|Refused to|blocked|securitypolicyviolation/i.test(t))console_msgs.push(t.slice(0,300));});
  page.on('pageerror',e=>pageerrors.push(String(e).slice(0,200)));
  const out={label,url,console:console_msgs,violations,pageerrors,steps:{}};
  try{
    // 主框架内的 CSP 违规事件（srcdoc 继承父 CSP，违规可能在主框架 Report 或在 iframe 内）
    await page.addInitScript(()=>{
      window.__cspViol=[];
      document.addEventListener('securitypolicyviolation',e=>{
        window.__cspViol.push({directive:e.effectiveDirective,blocked:e.blockedURI,
          sample:(e.sample||'').slice(0,80),source:e.sourceFile});
      },true);
    });
    await page.goto(url,{waitUntil:'domcontentloaded'});
    await page.waitForSelector('#btn-aifold',{timeout:25000});
    out.steps.mainUiBooted=true;
    await page.waitForTimeout(500);
    out.steps.mainViolations=await page.evaluate(()=>window.__cspViol||[]).catch(()=>[]);
    // 若 run 已 ready，直接点启动；否则先 AIFold
    const stage=await page.locator('#btn-aifold').getAttribute('data-stage').catch(()=>'?');
    out.steps.stageBefore=stage;
    if(stage==='off'){
      await page.locator('#btn-aifold').click();
      await page.waitForSelector('#btn-aifold[data-stage="ready"]',{timeout:30000}).catch(()=>{});
      out.steps.stageAfterAifold=await page.locator('#btn-aifold').getAttribute('data-stage').catch(()=>'?');
    }
    if(await page.locator('#btn-aifold[data-stage="ready"]').count()){
      await page.locator('#btn-aifold').click();   // 启动
    }
    await page.waitForTimeout(3000);
    out.steps.lvlbar=await page.locator('#lvlbar').count();
    out.steps.tfhosts=await page.locator('.rdrr-tfhost iframe').count();
    // 逐个 iframe 检查内核是否起来
    const frames=page.frames().filter(f=>f!==page.mainFrame());
    out.steps.frames=frames.length;
    out.steps.frameStates=[];
    for(const f of frames){
      const st=await f.evaluate(()=>{
        const out={hasReader:!!window.__reader,doc:!!document.getElementById('doc'),
          readyState:document.readyState};
        try{out.level=window.__reader?window.__reader.level:null;}catch(e){}
        out.docText=((document.getElementById('doc')||{}).innerText||'').slice(0,60);
        out.inlineScripts=document.querySelectorAll('script:not([src])').length;
        out.cspViol=window.__cspViol||[];
        return out;
      }).catch(e=>({evalError:String(e).slice(0,120)}));
      out.steps.frameStates.push(st);
    }
    // iframe 之间的 postMessage 事件是否到达宿主（bridge 若被阻断，宿主收不到）
    out.steps.readerReady=frames.length>0&&out.steps.frameStates.every(s=>s.hasReader===true);
  }catch(e){out.error=String(e).slice(0,200);}
  out.console=console_msgs;out.pageerrors=pageerrors;
  await page.screenshot({path:`tmp/reader-csp-${label}.png`}).catch(()=>{});
  await ctx.close();
  return out;
}

(async()=>{
 const browser=await chromium.launch({headless:true});
 const plain=await runOne(browser,PLAIN,'nocsp');
 const csp=await runOne(browser,CSP,'tauricsp');
 await browser.close();
 const verdict={
   plain_reader_ready:plain.steps.readerReady===true,
   csp_reader_ready:csp.steps.readerReady===true,
   csp_blocked_scripts:(csp.console||[]).filter(t=>/Refused to execute inline script|script-src/i.test(t)).length,
   csp_violations:(csp.steps.mainViolations||[]).concat(
     ...(csp.steps.frameStates||[]).map(s=>s.cspViol||[])).slice(0,10),
 };
 const payload=JSON.stringify({plain,csp,verdict},null,1);
 require('fs').writeFileSync(RESULT,payload);
 console.log('VERDICT='+JSON.stringify(verdict));
 process.exit(verdict.csp_reader_ready?0:1);
})().catch(e=>{console.error('FATAL',String(e).slice(0,300));process.exit(99);});
