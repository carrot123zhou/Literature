const {chromium}=require(process.env.PW_PACKAGE_PATH);

// 逐空间独立验收（5 场景 × 独立 context）：源 Key / 目标 Key / Takeaway / 源 Brief / 目标 derived Brief。
// argv: [web, api, pid, resultPath]
const WEB=`http://127.0.0.1:${process.argv[2]}`;
const API=`http://127.0.0.1:${process.argv[3]}`;
const PIDS=JSON.parse(process.argv[4]);
const PID=PIDS[0];
const RESULT=process.argv[5];

const assertions=[];
let CURRENT='?';
const ok=(name,pass,detail)=>{assertions.push({scenario:CURRENT,name:String(name),pass:!!pass,
  detail:detail===undefined?"":detail});return pass;};

// 帧信息记录：内核 level/lang（__reader 原生 getter）+ 可见正文
async function frameInfo(frame){
  return frame.evaluate(()=>{
    const doc=document.getElementById('doc');
    if(!doc)return {level:null,lang:null,staged:null,text:''};
    const r=window.__reader;
    let lang=r&&r.lang?r.lang:null;
    if(!lang){   // 辅助：仅当 getter 不可用时按 CJK/拉丁判断
      const raw=doc.innerText||'';
      const cjk=(raw.match(/[\u4e00-\u9fff]/g)||[]).length;
      const latin=(raw.match(/[A-Za-z]/g)||[]).length;
      lang=cjk>=latin&&cjk>0?'zh':(latin>0?'en':null);
    }
    return {level:r?r.level:null,lang,staged:r?!!r.staged:null,
            text:(doc.innerText||'').replace(/\s+/g,' ').slice(0,160)};
  }).catch(()=>({level:null,lang:null,staged:null,text:''}));
}
// 等引擎运动/分帧稳定（真实用户在 morph 中同样无法有效划词）
async function waitSettled(frame,engineLevel,timeout=8000){
  const deadline=Date.now()+timeout;
  let last={};
  while(Date.now()<deadline){
    last=await frame.evaluate(l=>{const r=window.__reader;
      return r?{level:r.level,pos:r.pos,staged:!!r.staged}:{};},engineLevel).catch(()=>({}));
    if(last.level===engineLevel&&last.staged===false&&Math.abs((last.pos??engineLevel)-engineLevel)<0.01)
      return {settled:true,...last};
    await new Promise(r=>setTimeout(r,150));
  }
  return {settled:false,...last};
}
// 帧定位：宿主 DOM pane → 内核 iframe（确定性）；语言/level 仅作记录核验
async function getFrame(page,pane,timeout=20000){
  const deadline=Date.now()+timeout;
  while(Date.now()<deadline){
    const handle=await page.$(`#rdrr-pane-${pane} .rdrr-tfhost iframe`);
    if(handle){
      const frame=await handle.contentFrame();
      if(frame){
        const ready=await frame.evaluate(()=>!!document.getElementById('doc')&&!!window.__reader).catch(()=>false);
        if(ready)return frame;
      }
    }
    await new Promise(r=>setTimeout(r,200));
  }
  return null;
}
// 内核 POST 原生锚捕获：bridge 替换 window.fetch 之后再包一层（只记录不拦截）
async function installCapture(frame){
  await frame.evaluate(()=>{
    if(window.__capInstalled)return;
    window.__capInstalled=true;
    const prev=window.fetch;
    window.__notePuts=[];
    window.fetch=function(input,init){
      try{
        const url=typeof input==='string'?input:(input&&input.url)||'';
        if(String(url).includes('/api/notes/')&&init&&init.body){
          const b=JSON.parse(init.body);
          if(b.op==='put')window.__notePuts.push({t:Date.now(),noteId:b.note&&b.note.id,
            anchor:b.note&&b.note.anchor||null});
        }
      }catch(e){}
      return prev(input,init);
    };
  });
}
// txt 模式真实文本 Range 选词（跨 text node，不要求 data-id）→ mouseup → 内核 selbar → 高光
// —— 选词：首轮与重试共用同一「真实可见 occurrence」判定，并逐次记录现场 ——
async function attemptSelect(frame,phrase,mode,attempt){
  const res=await frame.evaluate(({text,mode})=>{
    // 自包含 picker：遍历所有 occurrence，只保留 rects>0 且节点仍连接的可见位置
    const doc=document.getElementById('doc');
    const walker=document.createTreeWalker(doc,NodeFilter.SHOW_TEXT);
    let node;const parts=[];
    while(node=walker.nextNode())parts.push({node,text:node.textContent});
    const full=parts.map(p=>p.text).join('');
    const build=(idx)=>{
      let acc=0,sN=null,sO=0,eN=null,eO=0;
      for(const p of parts){
        const len=p.text.length;
        if(sN===null&&acc+len>idx){sN=p.node;sO=idx-acc;}
        if(acc<idx+text.length){eN=p.node;eO=idx+text.length-acc;}
        acc+=len;
        if(sN&&eN&&acc>=idx+text.length)break;
      }
      if(sN===null||eN===null)return null;
      const r=document.createRange();r.setStart(sN,sO);r.setEnd(eN,eO);
      const rects=r.getClientRects();
      const box=rects.length?r.getBoundingClientRect():null;
      return {range:r,startNode:sN,rects:rects.length,connected:sN.isConnected,
        rect:box?{x:Math.round(box.x),y:Math.round(box.y),w:Math.round(box.width),h:Math.round(box.height)}:null};
    };
    const occ=[];
    for(let i=0;i<full.length;i++){
      const at=full.indexOf(text,i);if(at<0)break;
      const b=build(at);
      if(b)occ.push({at,rects:b.rects,connected:b.connected,rect:b.rect,b});
      i=at;
    }
    const visible=occ.filter(o=>o.rects>0&&o.connected&&o.rect&&o.rect.w>1&&o.rect.h>1);
    const occSummary=occ.map(o=>({at:o.at,rects:o.rects,connected:o.connected,rect:o.rect}));
    if(!visible.length)return {ok:false,reason:'no visible occurrence',occ:occSummary};
    const chosen=visible[0];
    const sel=window.getSelection();sel.removeAllRanges();sel.addRange(chosen.b.range);
    const before={selToString:String(sel).slice(0,60),rects:chosen.b.range.getClientRects().length,
      connected:chosen.b.startNode.isConnected,rect:chosen.rect,
      frameUrl:(document.URL||'').slice(0,40),
      sameOwnerDoc:(document.getElementById('doc')||{}).ownerDocument===document};
    if(mode==='mouse')return {ok:true,mouse:true,rect:chosen.rect,chosen:chosen.at,occ:occSummary,visible:visible.length};
    doc.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,
      clientX:chosen.rect.x+chosen.rect.w/2,clientY:chosen.rect.y+chosen.rect.h/2}));
    return {ok:true,mouse:false,rect:chosen.rect,chosen:chosen.at,occ:occSummary,visible:visible.length};
  },{text:phrase,mode}).catch(e=>({ok:false,err:String(e).slice(0,120)}));
  await new Promise(r=>setTimeout(r,650));
  const after=await frame.evaluate(()=>{
    const sel=window.getSelection();
    const bar=document.querySelector('.selbar');
    let rects=0,str='';
    try{if(sel&&sel.rangeCount){rects=sel.getRangeAt(0).getClientRects().length;str=String(sel);}}catch(e){}
    return {selToString:str.slice(0,60),rangeRects:rects,
      selbar:bar?{hidden:bar.hidden}:{none:true}};
  }).catch(e=>({err:String(e).slice(0,80)}));
  return {attempt,mode,...res,after};
}
async function selectAndHighlight(page,frame,phrase,opts={}){
  const log=[]; let bar={};
  const modes=['range','mouse','mouse'];
  for(let i=0;i<modes.length;i++){
    const mode=modes[i];
    if(i>0){
      // 每次重试前清空选区，避免残留影响
      await frame.evaluate(()=>{try{window.getSelection().removeAllRanges();}catch(e){}}).catch(()=>{});
    }
    const a=await attemptSelect(frame,phrase,mode,i);
    log.push(a);
    if(mode==='mouse'&&a.ok&&a.rect){
      // 真实鼠标拖选（用户路径）：page 坐标 = 目标 pane iframe 偏移 + iframe 内字形坐标
      const iframeBox=await page.evaluate((wanted)=>{
        const el=document.querySelector(`#rdrr-pane-${wanted} .rdrr-tfhost iframe`);
        if(!el)return {x:0,y:0,pane:null};
        const r=el.getBoundingClientRect();return {x:r.x,y:r.y,pane:wanted};
      },opts.pane||'source');
      await frame.evaluate(()=>{try{window.getSelection().removeAllRanges();}catch(e){}}).catch(()=>{});
      const x1=iframeBox.x+a.rect.x-1, y1=iframeBox.y+a.rect.y+Math.max(2,Math.round(a.rect.h/2));
      const x2=iframeBox.x+a.rect.x+a.rect.w+1, y2=y1;
      await page.mouse.move(x1,y1);
      await page.mouse.down();
      for(let k=1;k<=10;k++){await page.mouse.move(x1+(x2-x1)*k/10,y1);}
      await page.mouse.up();
      await page.waitForTimeout(700);
      const afterMouse=await frame.evaluate(()=>{const sel=window.getSelection();
        const b=document.querySelector('.selbar');let rects=0,str='';
        try{if(sel&&sel.rangeCount){rects=sel.getRangeAt(0).getClientRects().length;str=String(sel);}}catch(e){}
        return {selToString:str.slice(0,60),rangeRects:rects,selbar:b?{hidden:b.hidden}:{none:true}};}).catch(()=>({}));
      a.afterMouse={geom:iframeBox,mouseRect:a.rect,after:afterMouse};
      if(afterMouse.selbar&&afterMouse.selbar.hidden===false){bar=afterMouse.selbar;break;}
      bar=afterMouse.selbar||{none:true};
      continue;
    }
    bar=a.after&&a.after.selbar?a.after.selbar:(await frame.evaluate(()=>{
      const b=document.querySelector('.selbar');return b?{hidden:b.hidden}:{none:true};}).catch(()=>({none:true})));
    if(a.ok&&bar&&bar.hidden===false)break;
  }
  const finalBar=await frame.evaluate(()=>{const b=document.querySelector('.selbar');
    return b?{hidden:b.hidden}:{none:true};}).catch(()=>({none:true}));
  return {selDone:log[0]||null,bar:finalBar,log};
}
const kernelNotes=async frame=>frame.evaluate(async()=>{const r=await fetch('/api/notes/');return r.json();});
const overlayState=async(page,pid)=>page.evaluate(async({api,pid})=>{
  const r=await fetch(`${api}/projects/${pid}/reader/state?locale=zh-CN`,{headers:{'x-literature-token':''}});
  return (await r.json()).overlay||{};
},{api:API,pid:pid||PID}).catch(()=>({}));
async function enterFoldPid(page,pid){
  await page.goto(`${WEB}/#/p/${pid}/reader`,{waitUntil:'domcontentloaded'}).catch(()=>{});
  await page.waitForSelector('#btn-aifold',{timeout:20000});
  return enterFold(page);
}
async function enterFold(page){
  const stage=await page.locator('#btn-aifold').getAttribute('data-stage').catch(()=>'?');
  if(stage==='on')return true;
  await page.locator('#btn-aifold').click();
  try{await page.waitForSelector('#lvlbar',{timeout:20000});return true;}
  catch(e){
    if(await page.locator('#btn-aifold[data-stage="ready"]').count()){
      await page.locator('#btn-aifold').click();
      await page.waitForSelector('#lvlbar',{timeout:20000});return true;
    }
    return false;
  }
}
async function levelOf(frame){
  return frame.evaluate(()=>({level:window.__reader?window.__reader.level:null,
    doc:document.getElementById('doc')?document.getElementById('doc').dataset.level:null}))
    .catch(()=>({level:null,doc:null}));
}
// 正式用户路径：只点宿主层级 bar；返回双栏实际层（不做 API 兜底）
async function clickBarLevel(page,frames,productLevel,retries=3){
  const want=5-productLevel;
  const attempts=[];
  for(let attempt=0;attempt<retries;attempt++){
    await page.locator(`#lvlbar [data-l="${productLevel}"]`).click();
    const deadline=Date.now()+2500;
    let srcLv=null,dstLv=null;
    while(Date.now()<deadline){
      srcLv=await levelOf(frames.src);
      dstLv=frames.dst?await levelOf(frames.dst):srcLv;
      if(srcLv.level===want&&dstLv.level===want)break;
      await page.waitForTimeout(120);
    }
    attempts.push({attempt,src:srcLv,dst:dstLv});
    if(srcLv.level===want&&dstLv.level===want){await page.waitForTimeout(400);
      return {synced:true,via:'bar',attempts};}
    await page.waitForTimeout(500);
  }
  return {synced:false,via:'bar',attempts};
}
// 诊断专用（仅在 bar 点击被记录为 FAIL 之后调用，不得用于判过）
async function diagnosticApiLevel(frames,productLevel){
  const want=5-productLevel;
  for(const f of [frames.src,frames.dst].filter(Boolean)){
    await f.evaluate(l=>{try{window.__reader.setLevel(l);}catch(e){}},want);
    await f.waitForFunction(l=>window.__reader&&window.__reader.level===l,want,{timeout:2500}).catch(()=>{});
  }
  await new Promise(r=>setTimeout(r,400));
  return {via:'api-diagnostic-fallback',src:await levelOf(frames.src),
          dst:frames.dst?await levelOf(frames.dst):null};
}
const reopenMark=async(page,pid,pane,productLevel,phrase)=>{
  await page.reload({waitUntil:'domcontentloaded'});
  await page.waitForSelector('#btn-aifold',{timeout:20000});
  await page.waitForTimeout(800);
  await enterFold(page);
  const src=await getFrame(page,'source');
  const dst=await getFrame(page,'trans');
  if(!src)return {err:'reopen: frame not found'};
  const sync=await clickBarLevel(page,{src,dst},productLevel);
  let via='bar';
  if(!sync.synced){   // 诊断兜底：已在上方断言记录 bar 失败，此处只保证后续取证可跑
    await diagnosticApiLevel({src,dst},productLevel);
    via='api-diagnostic-fallback';
  }
  await page.waitForTimeout(900);
  const frame=pane==='source'?src:dst||src;
  const out=await frame.evaluate(phrase=>{
    // 高亮串 = 从首个 .hl 到末个 .hl 的 Range.toString()（保留文档真实空格与 CJK，不人为插入/剥离）
    const doc=document.getElementById('doc');
    const spans=[...document.querySelectorAll('#doc .hl')];
    let joined='';
    if(spans.length){
      try{
        const r=document.createRange();
        r.setStartBefore(spans[0]);r.setEndAfter(spans[spans.length-1]);
        joined=r.toString().replace(/\s+/g,' ').trim();
      }catch(e){}
    }
    return {marks:spans.map(m=>m.innerText.slice(0,40)),
            joined,spanCount:spans.length,
            hit:joined.includes(phrase)?joined:null,
            exact:joined===phrase,
            level:window.__reader?window.__reader.level:null};
  },phrase).catch(e=>({err:String(e).slice(0,80)}));
  return {...out,levelSync:sync,levelVia:via};
};

let timeline=[];
const flush=()=>{
  const failed=assertions.filter(a=>!a.pass);
  try{require('fs').writeFileSync(RESULT,JSON.stringify(
    {assertions,failed:failed.length,timeline,incremental:true},null,1));}catch(e){}
};
(async()=>{
 const browser=await chromium.launch({headless:true});
 const attach=(page)=>{
   page.on('request',r=>{
     const u=r.url();if(!u.includes('/reader/'))return;
     timeline.push({t:Date.now(),dir:'req',method:r.method(),path:u.slice(API.length).split('?')[0].slice(0,90)});
     if(r.method()==='PUT'&&u.includes('/reader/overlay')){
       let rev=null,anns=[];
       try{const b=r.postDataJSON();rev=b.base_revision??b.overlay?.revision??null;
         anns=(b.overlay?.annotations||[]).map(a=>({id:a.id,anchor:a.anchor,pane:a.pane,
           block_id:a.block_id||null,quote:(a.quote&&a.quote.exact||'').slice(0,40),
           engine_anchor:a.engine_anchor?{space:a.engine_anchor.space,digest:a.engine_anchor.digest,
             version:a.engine_anchor.version,anchor:a.engine_anchor.anchor}:null}));}catch(e){}
       timeline.push({t:Date.now(),dir:'put-body',revision:rev,annotations:anns});
     }
   });
   page.on('response',async r=>{
     const u=r.url();if(!u.includes('/reader/'))return;
     const rec={t:Date.now(),dir:'resp',method:r.request().method(),
       path:u.slice(API.length).split('?')[0].slice(0,90),status:r.status()};
     if(r.request().method()==='PUT'&&u.includes('/reader/overlay')){
       try{const body=await r.json();
         rec.savedAnnotations=(body.annotations||[]).map(a=>({id:a.id,space:a.engine_anchor?.space,
           hasEa:!!a.engine_anchor,digest:a.engine_anchor?.digest}));
         rec.savedRevision=body.revision;
         rec.dropped=rec.savedAnnotations.filter(a=>!a.hasEa).map(a=>a.id);
       }catch(e){rec.bodyErr=String(e).slice(0,60);}
     }
     timeline.push(rec);
   });
   page.on('pageerror',e=>timeline.push({t:Date.now(),kind:'pageerror',message:String(e).slice(0,300)}));
 };

 const ONLY=process.env.PS_ONLY||'';
 // 默认走真实鼠标拖选（用户路径）；PS_RANGE=1 可退回程序化 Range 仅作调试
 const FORCE_MOUSE=process.env.PS_RANGE!=='1';
 const ONLY_NO_PANES=false;
 const SCENARIOS=[
  {name:'src-key',pane:'source',level:3,space:'brief',phrase:'Demand fell after the ninth century',withM:true},
  // 独立夹具（项目 B）：unit takeaway ≠ 章节 takeaway，L1 可见真正的 unit 写行
  {name:'unit-takeaway',project:1,pane:'source',level:4,space:'brief',
   phrase:'Unit takeaway: hoards cluster in port cities',expectUnitTakeaway:true},
  {name:'dst-key',pane:'trans',level:3,space:'brief',phrase:'依据证据句',withM:false},
  {name:'src-brief',pane:'source',level:2,space:'full',phrase:'Silver vessels travelled along the Silk Road',withM:true},
  {name:'dst-derived',pane:'trans',level:2,space:'derived',phrase:'这是分层译句',withM:false},
  // 章节空间：summary（源/目标）与 title（源）——summary 的 tokens 索引 chunk.lad.take
  {name:'summary-src',pane:'source',level:4,space:'summary',phrase:'Silver vessels travelled',expectSummary:true},
  {name:'summary-dst',pane:'trans',level:4,space:'summary',phrase:'本节总结白银传播',expectSummary:true},
  {name:'title-src',pane:'source',level:1,space:'title',phrase:'Introduction',expectTitle:true},
 ];

 // —— 偏好尊重（产品本次唯一改动 panes.trans 默认 True）：显式 False 必须保留 ——
 if(!ONLY||ONLY==='pref-trans-off'){
  await (async()=>{
   CURRENT='perspace/pref-trans-off';
   let page;
   try{
    const pid=PIDS[0];
    const ctx=await browser.newContext({viewport:{width:1440,height:920}});
    page=await ctx.newPage();attach(page);
    // 先写入显式 trans=false 的 overlay
    await page.goto(`${WEB}/#/p/${pid}/reader`,{waitUntil:'domcontentloaded'});
    await page.waitForSelector('#btn-aifold',{timeout:20000});
    await page.evaluate(async({api,pid})=>{
      const st=await fetch(`${api}/projects/${pid}/reader/state?locale=zh-CN`,
        {headers:{'x-literature-token':''}}).then(r=>r.json());
      const ov=st.overlay||{version:1,revision:0,annotations:[],prefs:{}};
      const prefs={...(ov.prefs||{}),panes:{...(ov.prefs?.panes||{}),origin:true,source:true,trans:false}};
      await fetch(`${api}/projects/${pid}/reader/overlay`,{method:'PUT',
        headers:{'x-literature-token':'','content-type':'application/json'},
        body:JSON.stringify({base_revision:ov.revision,
          overlay:{...ov,prefs}}) });
    },{api:API,pid});
    await page.reload({waitUntil:'domcontentloaded'});
    await page.waitForSelector('#btn-aifold',{timeout:20000});
    await page.waitForTimeout(900);
    const state=await page.evaluate(()=>{
      const lab=[...document.querySelectorAll('.rdrr-toggles label')]
        .find(l=>l.textContent.includes('译文'));
      const cb=lab?lab.querySelector('input[type="checkbox"]'):null;
      const iframe=document.querySelector('#rdrr-pane-trans .rdrr-tfhost iframe');
      const r=iframe?iframe.getBoundingClientRect():null;
      return {checked:cb?cb.checked:null,transHidden:document.getElementById('rdrr-pane-trans')?.classList.contains('hidden')??null,
        iframeW:r?Math.round(r.width):null,
        prefs:null};
    });
    ok('显式 trans=false 被尊重（不因默认值改动被强制开启）',
       state.checked===false&&state.transHidden===true&&(state.iframeW==null||state.iframeW===0),state);
    await ctx.close();
   }catch(e){ok('显式 trans=false 被尊重（不因默认值改动被强制开启）',false,String(e).slice(0,200));
     if(page)await page.context().close().catch(()=>{});}
  })();
 }

 for(const sc of SCENARIOS.filter(x=>!ONLY||x.name===ONLY)){
  await (async()=>{
   CURRENT='perspace/'+sc.name;
   let page;
   try{
    const ctx=await browser.newContext({viewport:{width:1440,height:920}});
    page=await ctx.newPage();attach(page);
    const scPid=PIDS[sc.project||0];
    await page.goto(`${WEB}/#/p/${scPid}/reader`,{waitUntil:'domcontentloaded'});
    await page.waitForSelector('#btn-aifold',{timeout:20000});
    await page.waitForTimeout(600);
    const entered=await enterFold(page);
    ok('enter fold（run ready 复用，不重复 POST）',entered,
       {stage:await page.locator('#btn-aifold').getAttribute('data-stage').catch(()=>'?')});
    await page.waitForTimeout(1200);
    if(sc.pane==='trans' && !ONLY_NO_PANES){
      // 真实用户路径：译文栏默认未勾选 → 先勾选，并等 iframe 得到非零尺寸
      const transToggle=page.locator('.rdrr-toggles label').filter({hasText:'译文'}).first();
      const box=await transToggle.locator('input[type="checkbox"]').isChecked().catch(()=>true);
      if(!box){await transToggle.click();await page.waitForTimeout(600);}
      for(let i=0;i<20;i++){
        const ok=await page.evaluate(()=>{
          const el=document.querySelector('#rdrr-pane-trans .rdrr-tfhost iframe');
          if(!el)return false;const r=el.getBoundingClientRect();return r.width>50&&r.height>50;
        }).catch(()=>false);
        if(ok)break;await page.waitForTimeout(200);
      }
    }
    const srcF=await getFrame(page,'source');
    const dstF=await getFrame(page,'trans');
    let frame=sc.pane==='source'?srcF:(dstF||srcF);
    if(!frame){ok('帧定位（pane）',false,'frame not found');await ctx.close();return;}
    await installCapture(frame);
    // 正式用户路径：点击宿主层级 bar，双栏实际层必须一致（独立断言，不用 API 兜底）
    let levelSync=await clickBarLevel(page,{src:srcF,dst:dstF},sc.level);
    let levelVia='bar';
    ok('宿主 bar 点击后双栏实际层一致（正式用户路径）',
       levelSync.synced,{want:5-sc.level,attempts:levelSync.attempts});
    if(!levelSync.synced){
      const diag=await diagnosticApiLevel({src:srcF,dst:dstF},sc.level);
      levelVia='api-diagnostic-fallback';
      ok('【诊断】bar 未同步时引擎 API 可否切层（仅定位用，不判过）',
         diag.src?.level===5-sc.level&&(!dstF||diag.dst?.level===5-sc.level),diag);
    }
    const settle=await waitSettled(frame,5-sc.level);
    ok('选词前引擎运动已稳定（staged=false、pos=目标层）',settle.settled,settle);
    await page.waitForTimeout(200);
    const info=await frameInfo(frame);
    ok('iframe 引擎层与可见正文（pane 定位后核验）',
       info.level===5-sc.level&&info.text.length>10,{...info,levelVia,levelSync});

    // 诊断（只读）：宿主侧目标 pane/iframe 的真实布局状态 + prefs 实际值（定位 hidden 来源）
    const hostPaneState=await page.evaluate(()=>{
      const pane=document.querySelector('#rdrr-pane-trans');
      const host=pane?pane.querySelector('.rdrr-tfhost'):null;
      const iframe=pane?pane.querySelector('.rdrr-tfhost iframe'):null;
      const cs=id=>{const el=document.getElementById(id);if(!el)return null;const c=getComputedStyle(el);return {display:c.display,visibility:c.visibility,w:el.getBoundingClientRect().width,h:el.getBoundingClientRect().height};};
      // 从 React 状态不可直达——读 localStorage/服务端 overlay prefs 的 UI 痕迹：
      // 三个选框 checkbox 的 checked 即 prefs.panes 的当前值（受控组件）。
      const toggles=[...document.querySelectorAll('.rdrr-ptoggle input')].map(cb=>({aria:cb.getAttribute('aria-label'),checked:cb.checked}));
      return {paneCls:pane?pane.className:null,paneDisplay:cs('rdrr-pane-trans'),
        hostDisplay:host?getComputedStyle(host).display:null,
        iframeRect:iframe?(()=>{const r=iframe.getBoundingClientRect();return {w:Math.round(r.width),h:Math.round(r.height)}})():null,
        mainW:(document.getElementById('rdrr-main')||{}).clientWidth,
        narrow:window.matchMedia('(max-width:899px)').matches,
        toggles,
        paneSourceCls:document.getElementById('rdrr-pane-source')?.className||null};
    }).catch(e=>({err:String(e).slice(0,120)}));
    const serverPrefs=await page.evaluate(async({api,pid})=>{
      const r=await fetch(`${api}/projects/${pid}/reader/overlay`,{headers:{'x-literature-token':''}});
      const d=await r.json();
      return {prefs:d.prefs||null,revision:d.revision,anns:(d.annotations||[]).length};
    },{api:API,pid:PIDS[sc.project||0]}).catch(e=>({err:String(e).slice(0,120)}));
    hostPaneState.serverPrefs=serverPrefs;
    ok('【诊断】宿主目标 pane 布局状态',true,hostPaneState);
    let r=await selectAndHighlight(page,frame,sc.phrase,{mouse:FORCE_MOUSE,pane:sc.pane});
    if(!(r.bar&&r.bar.hidden===false)){
      // 真实鼠标拖选对照（用户路径）：同一可见性 picker；若所有 occurrence 都 rects=0 则无坐标可拖
      const mouseProbe=await attemptSelect(frame,sc.phrase,'mouse',99);
      r={...r,mouseProbe};
    }
    await page.waitForTimeout(900);
    // selbar 出现 → 点「高亮」按钮触发内核 notes POST（与用户点击高亮同一路径）；
    // 无此步骤脚本从未触发 POST（此前只有选区+selbar）。
    const hlClick=await frame.evaluate(()=>{
      const b=document.querySelector('.selbar button[data-act="hl"]');
      if(!b||b.closest('.selbar')?.hidden)return {clicked:false,why:'selbar hidden/无按钮'};
      b.click();return {clicked:true};
    }).catch(e=>({clicked:false,err:String(e).slice(0,80)}));
    await page.waitForTimeout(900);
    ok('【诊断】selbar 高亮按钮点击',true,hlClick);
    const kNotes=await kernelNotes(frame);
    const notePuts=await frame.evaluate(()=>window.__notePuts||[]).catch(()=>[]);
    // 内核 /api/notes GET 不带宿主信封（toEngineNote 把信封内原生 anchor 还原为 note.anchor），
    // 因此按 note.anchor.on 区分空间，不依赖 engine_anchor.space。
    const sameSpace=n=>{
      const on=n.anchor?.on;
      if(sc.expectSummary)return on==='summary';
      if(sc.expectTitle)return on==='title';
      return on!=='summary'&&on!=='title';
    };
    const candidates=(kNotes.notes||[]).filter(n=>(n.anchor?.quote?.exact||'').includes(sc.phrase));
    const mine=candidates.filter(sameSpace).pop()||candidates.pop()||null;
    if(!mine&&!notePuts.length){
      // 无 POST：记录现场（selection/iframe 真实 lang+level/可见正文/按钮状态）
      const scene=await frame.evaluate(()=>{
        const sel=window.getSelection();
        const bar=document.querySelector('.selbar');
        const doc=document.getElementById('doc');
        // DOM 文本节点 + Range 前后字符偏移（供修复者定位 selection 被清空/未命中的具体位置）
        let off=null,nodeInfo=null;
        if(sel&&sel.rangeCount){
          const r=sel.getRangeAt(0);
          const w=document.createTreeWalker(doc,NodeFilter.SHOW_TEXT);
          let n;let acc=0;let startOff=null,endOff=null;const parts=[];
          while(n=w.nextNode()){
            const len=(n.textContent||'').length;
            if(startOff===null&&n===r.startContainer)startOff=acc+r.startOffset;
            if(n===r.endContainer)endOff=acc+r.endOffset;
            parts.push({node:(n.textContent||'').slice(0,40),len});
            acc+=len;
          }
          off={start:startOff,end:endOff,docTextLen:acc};
          nodeInfo={startContainer:r.startContainer.nodeName,
            startText:(r.startContainer.textContent||'').slice(0,60),
            endContainer:r.endContainer.nodeName,
            endText:(r.endContainer.textContent||'').slice(0,60)};
        }
        let rangeRects=null,selRects=null,ancestors=null;
        if(sel&&sel.rangeCount){
          const r0=sel.getRangeAt(0);
          try{rangeRects=r0.getClientRects().length;}catch(e){}
          try{selRects=sel.getRangeAt(0).getBoundingClientRect();}catch(e){}
        }
        if(sel&&sel.anchorNode){
          // 祖先链的 display/visibility/opacity（判断“不可见节点被选中”）
          const chain=[];let el=sel.anchorNode.nodeType===1?sel.anchorNode:sel.anchorNode.parentElement;
          for(let i=0;i<6&&el;i++){
            const cs=getComputedStyle(el);
            chain.push({tag:el.tagName,id:el.id||null,cls:(el.className||'').toString().slice(0,30),
              display:cs.display,visibility:cs.visibility,opacity:cs.opacity,hidden:!!el.hidden});
            el=el.parentElement;
          }
          ancestors=chain;
        }
        const u0=document.querySelector('#doc .u');
        const geom={docW:doc?doc.clientWidth:null,docH:doc?doc.clientHeight:null,
          docScrollW:doc?doc.scrollWidth:null,bodyW:document.body?document.body.clientWidth:null,
          unitW:u0?u0.offsetWidth:null,unitH:u0?u0.offsetHeight:null,
          unitMode:u0?u0._mode||null:null,
          htmlW:document.documentElement.clientWidth,htmlH:document.documentElement.clientHeight,
          innerW:window.innerWidth,innerH:window.innerHeight,
          frameEl:(()=>{try{const fe=window.frameElement;
            if(!fe)return null;const r=fe.getBoundingClientRect();
            const cs=getComputedStyle(fe);
            const host=(fe.parentElement&&fe.parentElement.parentElement)||null;
            const hcs=host?getComputedStyle(host):null;
            const hr=host?host.getBoundingClientRect():null;
            return {w:Math.round(r.width),h:Math.round(r.height),display:cs.display,
              hostClass:host?(host.className||'').toString().slice(0,40):null,
              hostW:hr?Math.round(hr.width):null,hostH:hr?Math.round(hr.height):null,
              hostDisplay:hcs?hcs.display:null};}catch(e){return {err:String(e).slice(0,60)};}})()};
        return {geom,selection:sel?sel.toString().replace(/\s+/g,' ').slice(0,120):null,
                rangeToString:sel&&sel.rangeCount?(()=>{try{return sel.getRangeAt(0).toString().replace(/\s+/g,' ').slice(0,120);}catch(e){return null;}})():null,
                rangeRects,selRect:selRects,ancestors,
                collapsed:sel?sel.isCollapsed:null,rangeCount:sel?sel.rangeCount:null,
                offsets:off,rangeNodes:nodeInfo,
                lang:window.__reader?window.__reader.lang:null,
                level:window.__reader?window.__reader.level:null,
                staged:window.__reader?!!window.__reader.staged:null,
                pos:window.__reader?window.__reader.pos:null,
                visible:(doc||{}).innerText?.replace(/\s+/g,' ').slice(0,140),
                markCount:doc?doc.querySelectorAll('.hl').length:null,
                selbar:bar?{hidden:bar.hidden,buttons:[...bar.querySelectorAll('button')].map(b=>b.dataset.act)}:null};
      }).catch(e=>({err:String(e).slice(0,80)}));
      ok('【现场】selbar 失败：selection/iframe/按钮状态',false,
         {scene,sel:r.selDone,bar:r.bar,mouseProbe:r.mouseProbe||null,levelVia});
    } else {
      ok('自然 selbar 选词保存：内核笔记存在（quote=可见选区）',!!mine,
         {sel:{selDone:r.selDone,bar:r.bar},
         note:mine&&{id:mine.id,text:(mine.text||'').slice(0,40),
           anchor:{on:mine.anchor?.on,unit:mine.anchor?.unit,section:mine.anchor?.section,
             lang:mine.anchor?.lang,level:mine.anchor?.level,tokens:mine.anchor?.tokens,
             sents:mine.anchor?.sents,shown:mine.anchor?.shown,quote:mine.anchor?.quote?.exact}},
         posted:notePuts,levelVia});
    }

    // overlay 保存有防抖：轮询等到该 note id 落盘（或超时）
    let ov={};
    let ovAnn=null;
    for(let i=0;i<16;i++){
      ov=await overlayState(page,scPid);
      ovAnn=(ov.annotations||[]).find(a=>mine&&a.id===mine.id)
        ||(ov.annotations||[]).find(a=>(a.quote?.exact||'').includes(sc.phrase))||null;
      if(ovAnn&&ovAnn.engine_anchor)break;
      await page.waitForTimeout(300);
    }
    const putBody=timeline.filter(e=>e.dir==='put-body'&&e.annotations&&
      e.annotations.some(a=>a.id===(mine?mine.id:'##'))).pop()||null;
    const putAnn=putBody&&putBody.annotations?putBody.annotations.find(a=>a.id===mine.id):null;
    // 按空间分派校验（旧 unit 空间规则不变；summary/title 不要求 unit/shown）
    const ea=ovAnn&&ovAnn.engine_anchor||null;
    const dig8=ea&&/^[0-9a-f]{8}$/.test(ea.digest||'');
    let spaceOk=false,spaceDetail=null;
    if(sc.expectSummary){
      const a=ea&&ea.anchor||{};
      spaceOk=!!ea&&ea.space==='summary'&&a.on==='summary'&&a.unit==null
        &&!!a.section&&Array.isArray(a.tokens)&&!a.shown&&(a.sents===null||a.sents===undefined)&&dig8;
      spaceDetail={space:ea?.space,on:a.on,unit:a.unit,section:a.section,tokens:a.tokens,
        shown:!!a.shown,sents:a.sents};
    }else if(sc.expectTitle){
      const a=ea&&ea.anchor||{};
      spaceOk=!!ea&&ea.space==='title'&&!a.tokens&&dig8;
      spaceDetail={space:ea?.space,tokens:a.tokens??null,digest:ea?.digest,block_id:ovAnn?.block_id};
    }else if(sc.expectUnitTakeaway){
      const a=ea&&ea.anchor||{};
      spaceOk=!!ea&&ea.space==='brief'&&a.on==='unit'
        &&typeof a.unit==='string'&&a.unit.length>0&&!!a.shown&&dig8;
      spaceDetail={space:ea?.space,on:a.on,unit:a.unit,shown:!!a.shown,digest:ea?.digest};
    }else{
      spaceOk=!!ea&&ea.space===sc.space&&dig8;
      spaceDetail={space:ea?.space,digest:ea?.digest};
    }
    // 逐字段对应：内核 note.anchor（GET 回执，无信封）↔ overlay.engine_anchor.anchor
    const noteAnchor=mine&&mine.anchor||null;
    const envAnchor=ea&&ea.anchor||null;
    const fieldCompare=noteAnchor&&envAnchor?{
      on:[noteAnchor.on,envAnchor.on],unit:[noteAnchor.unit,envAnchor.unit],
      section:[noteAnchor.section,envAnchor.section],lang:[noteAnchor.lang,envAnchor.lang],
      level:[noteAnchor.level,envAnchor.level],tokens:[noteAnchor.tokens,envAnchor.tokens],
      sents:[noteAnchor.sents,envAnchor.sents],
      shown:[noteAnchor.shown&&noteAnchor.shown.words,envAnchor.shown&&envAnchor.shown.words],
      quote:[(noteAnchor.quote&&noteAnchor.quote.exact||'').slice(0,40),
             (envAnchor.quote&&envAnchor.quote.exact||'').slice(0,40)],
      match:{on:noteAnchor.on===envAnchor.on,unit:noteAnchor.unit===envAnchor.unit,
        section:noteAnchor.section===envAnchor.section,lang:noteAnchor.lang===envAnchor.lang,
        level:noteAnchor.level===envAnchor.level,
        tokens:JSON.stringify(noteAnchor.tokens)===JSON.stringify(envAnchor.tokens),
        sents:JSON.stringify(noteAnchor.sents)===JSON.stringify(envAnchor.sents),
        shown:JSON.stringify(noteAnchor.shown&&noteAnchor.shown.words)===JSON.stringify(envAnchor.shown&&envAnchor.shown.words),
        quote:(noteAnchor.quote&&noteAnchor.quote.exact||'')===(envAnchor.quote&&envAnchor.quote.exact||'')},
    }:null;
    ok('宿主 PUT 信封符合该空间 schema（unit full/brief/derived 规则不变；summary/title 分别校验）',
       spaceOk,{want:sc.space,spaceDetail,putAnn:putAnn&&putAnn.engine_anchor,
         readback:ovAnn&&{id:ovAnn.id,block_id:ovAnn.block_id,anchor:ovAnn.anchor,
           hasEa:!!ovAnn.engine_anchor,space2:ovAnn.engine_anchor?.space},
         overlayCount:(ov.annotations||[]).length,
         overlayIds:(ov.annotations||[]).map(a=>a.id).slice(0,8),
         mineId:mine&&mine.id,scPid,
         noteVsEnvelope:fieldCompare});

    const re=await reopenMark(page,scPid,sc.pane,sc.level,sc.phrase);
    // 诊断：恢复后内核实际持有的笔记（区分 boot 时序 vs marksFor/写行校验）
    const reopenFrame=await getFrame(page,sc.pane).catch(()=>null);
    let reopenNotes={err:'frame lost'};
    if(reopenFrame){
      reopenNotes=await reopenFrame.evaluate(async()=>{
        try{
          const r=await fetch('/api/notes/');const d=await r.json();
          const u=document.getElementById('b-p-1');
          return {notes:(d.notes||[]).map(n=>({level:n.anchor?.level,lang:n.anchor?.lang,
            tokens:n.anchor?.tokens,sents:n.anchor?.sents,
            shown:n.anchor?.shown?{words:n.anchor.shown.words,exact:(n.anchor.shown.exact||'').slice(0,40)}:null,
            quote:(n.anchor?.quote?.exact||'').slice(0,40),on:n.anchor?.on})),
            tkCount:document.querySelectorAll('#b-p-1 .tk').length,
            hasNotes:u?u.classList.contains('has-notes'):null};
        }catch(e){return {err:String(e).slice(0,100)};}
      }).catch(e=>({err:String(e).slice(0,100)}));
    }
    ok('【诊断】恢复后内核笔记状态',true,reopenNotes||{err:'frame lost'});
    if(sc.expectTitle){
      // title 空间无 token 流 → 不要求高亮；只判笔记存续且没有把它错标到别处
      const ovAfter=await overlayState(page,scPid);
      const stillStored=(ovAfter.annotations||[]).some(a=>mine&&a.id===mine.id);
      // 其它笔记（summary/unit）的 mark 不应算作错标：只禁止标题文本被错标高亮
      const titleMarked=(re.marks||[]).some(t=>String(t).includes('Introduction'));
      ok('title 空间：刷新后笔记存续（无 token 流，不要求高亮；标题不被错标高亮）',
         stillStored&&!titleMarked,{stillStored,marks:re.marks,titleMarked});
    }else{
      ok('刷新同层可见精确 mark（高亮=所选词，非整段）',
         !!re.hit&&re.hit.length<=sc.phrase.length+12,re);
    }

    await page.screenshot({path:`tmp/reader-perspace-screens/${sc.name}.png`});
    await ctx.close();
    flush();
   }catch(e){ok('场景异常',false,String(e).slice(0,300)+(e.stack?String(e.stack).slice(0,150):''));
     if(page)await page.context().close().catch(()=>{});flush();}
  })();
 }

 await browser.close();
 const failed=assertions.filter(a=>!a.pass);
 const payload=JSON.stringify({assertions,failed:failed.length,timeline},null,1);
 try{require('fs').writeFileSync(RESULT,payload);}catch(e){console.log(payload.slice(0,3000));}
 console.log('PERSPACE_FAILED='+failed.length+' total='+assertions.length);
 process.exit(failed.length?1:0);
})().catch(e=>{console.error('FATAL',String(e).slice(0,400));process.exit(99);});
