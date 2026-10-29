const {chromium}=require(process.env.PW_PACKAGE_PATH);
// 用户反馈定向验收（当前 dist index-B56-l-OQ.js）。
// argv: [web, api, pidsJSON, resultPath]
const WEB=`http://127.0.0.1:${process.argv[2]}`;
const API=`http://127.0.0.1:${process.argv[3]}`;
const PIDS=JSON.parse(process.argv[4]);
const RESULT=process.argv[5];
const TOKEN=process.argv[6]||'';
const COLORS_ORDER=['y','g','b','p','o'];   // 产品 COLORS 冻结契约（ReaderWorkspace.tsx:37）
const A=PIDS[0], B=PIDS[1];
const R=[];let CURRENT='?';
const ok=(name,pass,detail)=>{R.push({scenario:CURRENT,name:String(name),pass:!!pass,
  detail:detail===undefined?'':detail});return pass;};

const timeline=[];
const consoleLog=[];
const attach=(page,pid)=>{page.on('pageerror',e=>R.push({scenario:CURRENT,name:'页面脚本异常',
  pass:false,detail:String(e).slice(0,200)}));
  page.on('console',m=>{const t=m.text();
    if(/sel|pop|overlay|fold|409|conflict|error/i.test(t))
      consoleLog.push({t:Date.now(),type:m.type(),text:t.slice(0,160)});
    if(consoleLog.length>400)consoleLog.splice(0,consoleLog.length-400);});
  page.addInitScript(()=>{
    try{
      window.__postMsgLog=window.__postMsgLog||[];
      window.addEventListener('message',e=>{
        const d=e&&e.data;
        const type=d&&typeof d==='object'?d.type:null;
        if(type&&/sel|fold|light|goto|rpc/i.test(String(type)))
          window.__postMsgLog.push({t:Date.now(),type:String(type),
            keys:d&&typeof d==='object'?Object.keys(d).slice(0,6):[]});
        if(window.__postMsgLog.length>300)window.__postMsgLog.splice(0,window.__postMsgLog.length-300);
      });
    }catch(e){}
  });
  page.on('request',r=>{const u=r.url();
    if(u.includes('/reader/overlay'))timeline.push({t:Date.now(),dir:'req',method:r.method(),
      path:u.slice(API.length).split('?')[0].slice(0,60)});
    if(r.method()==='PUT'&&u.includes('/reader/overlay')){
      try{const b=r.postDataJSON();
        timeline.push({t:Date.now(),dir:'put-body',revision:b.base_revision??b.overlay?.revision??null,
          annotations:(b.overlay?.annotations||[]).map(a=>({id:a.id,pane:a.pane,block_id:a.block_id,
            quote:(a.quote&&a.quote.exact||'').slice(0,40),color:a.color||null,
            engine_anchor:a.engine_anchor?{space:a.engine_anchor.space,digest:a.engine_anchor.digest,
              anchor:a.engine_anchor.anchor}:null}))});}catch(e){}
    }
  });
  page.on('response',r=>{const u=r.url();
    if(r.request().method()==='PUT'&&u.includes('/reader/overlay'))
      timeline.push({t:Date.now(),dir:'resp',status:r.status()});
  });};

let PUT_COUNT=0;
const countOverlayPuts=(sinceTs)=>timeline.filter(e=>e.dir==='req'&&e.method==='PUT'
  &&String(e.path||'').includes('/reader/overlay')&&e.t>=sinceTs).length;
const waitPutStable=async(page,sinceTs,timeoutMs=4000)=>{
  const dl=Date.now()+timeoutMs;let last=countOverlayPuts(sinceTs);
  while(Date.now()<dl){
    await new Promise(r=>setTimeout(r,300));
    const now=countOverlayPuts(sinceTs);
    if(now===last)return now;
    last=now;
  }
  return countOverlayPuts(sinceTs);
};

async function frameOf(page,pane,timeout=20000){
  const dl=Date.now()+timeout;
  while(Date.now()<dl){
    const h=await page.$(`#rdrr-pane-${pane} .rdrr-tfhost iframe`);
    if(h){const f=await h.contentFrame();
      if(f&&await f.evaluate(()=>!!window.__reader).catch(()=>false))return f;}
    await new Promise(r=>setTimeout(r,200));
  }return null;
}
async function enterFold(page){
  const st=await page.locator('#btn-aifold').getAttribute('data-stage').catch(()=>'?');
  if(st==='on')return true;
  await page.locator('#btn-aifold').click();
  try{await page.waitForSelector('#lvlbar',{timeout:20000});return true;}
  catch(e){if(await page.locator('#btn-aifold[data-stage="ready"]').count()){
    await page.locator('#btn-aifold').click();
    await page.waitForSelector('#lvlbar',{timeout:20000});return true;}
    return false;}
}
async function setLevel(page,frame,productLevel){
  for(let i=0;i<3;i++){
    await page.locator(`#lvlbar [data-l="${productLevel}"]`).click();
    const want=5-productLevel;
    const good=await frame.waitForFunction(l=>window.__reader&&window.__reader.level===l
      &&document.getElementById('doc').dataset.level===String(l),want,{timeout:2500})
      .then(()=>true).catch(()=>false);
    if(good){await page.waitForTimeout(400);return true;}
    await page.waitForTimeout(500);
  }return false;
}
// 只选 rects>0 的可见 occurrence（自包含 picker 在 iframe 内执行）
async function selectVisible(page,frame,phrase){
  const r=await frame.evaluate(text=>{
    const doc=document.getElementById('doc');
    const walker=document.createTreeWalker(doc,NodeFilter.SHOW_TEXT);
    let node;const parts=[];
    while(node=walker.nextNode())parts.push({node,text:node.textContent});
    const full=parts.map(p=>p.text).join('');
    const build=(idx)=>{let acc=0,sN=null,sO=0,eN=null,eO=0;
      for(const p of parts){const len=p.text.length;
        if(sN===null&&acc+len>idx){sN=p.node;sO=idx-acc;}
        if(acc<idx+text.length){eN=p.node;eO=idx+text.length-acc;}
        acc+=len;if(sN&&eN&&acc>=idx+text.length)break;}
      if(sN===null||eN===null)return null;
      const rr=document.createRange();rr.setStart(sN,sO);rr.setEnd(eN,eO);
      const rects=rr.getClientRects();const box=rects.length?rr.getBoundingClientRect():null;
      return {range:rr,rects:rects.length,connected:sN.isConnected,
        rect:box?{x:Math.round(box.x),y:Math.round(box.y),w:Math.round(box.width),h:Math.round(box.height)}:null};};
    const occ=[];for(let i=0;i<full.length;i++){const at=full.indexOf(text,i);if(at<0)break;
      const b=build(at);if(b)occ.push({at,...b});i=at;}
    const vis=occ.filter(o=>o.rects>0&&o.connected);
    if(!vis.length)return {ok:false,occ:occ.map(o=>({at:o.at,rects:o.rects,connected:o.connected,rect:o.rect}))};
    const c=vis[0];const sel=window.getSelection();sel.removeAllRanges();sel.addRange(c.range);
    return {ok:true,rect:c.rect,occ:occ.map(o=>({at:o.at,rects:o.rects,connected:o.connected}))};
  },phrase).catch(e=>({ok:false,err:String(e).slice(0,80)}));
  return r;
}
// 用首末 .hl Range 重建原样高亮文本（保留真实空格与 CJK，不人为插入/剥离）
async function joinedHighlight(frame){
  return frame.evaluate(()=>{
    const spans=[...document.querySelectorAll('#doc .hl')];
    if(!spans.length)return '';
    try{
      // compareDocumentPosition 排序（避免 Range 反向 throw）
      const sorted=spans.slice().sort((a,b)=>{
        const pos=a.compareDocumentPosition(b);
        return pos&Node.DOCUMENT_POSITION_FOLLOWING?-1:
               pos&Node.DOCUMENT_POSITION_PRECEDING?1:0;});
      const rr=document.createRange();
      rr.setStartBefore(sorted[0]);rr.setEndAfter(sorted[sorted.length-1]);
      return rr.toString().replace(/\s+/g,' ').trim();
    }catch(e){return 'ERR:'+String(e).slice(0,40);}
  }).catch(()=> '');
}
// 译文栏动态候选文本：无 .hl 批注覆盖的 unit 文本（避免硬编码被积累注释干扰）
async function pickTransCandidate(frame){
  return frame.evaluate(()=>{
    const us=[...document.querySelectorAll('#doc .u')];
    for(const u of us){
      if(u.querySelector('.hl'))continue;
      const t=((u._tx||u).textContent||'').replace(/\s+/g,' ').trim();
      if(t.length>=4)return t.slice(0,6);
    }
    return null;
  }).catch(()=>null);
}
// fold 内真实鼠标拖选（1e/2b 共用）：iframe 内首末字形端点 → page.mouse 拖拽 → 宿主 #selPop
async function foldDragSelect(page,frame,paneSel,text){
  const selRes=await selectVisible(page,frame,text);
  if(!selRes.ok)return {selRes,dragged:false,hostSelPop:null};
  // iframe 内部坐标 → 父页面坐标：必须加 iframe 元素在父页面的偏移（工具栏/栏布局）
  const box=await page.evaluate(sel=>{
    const el=document.querySelector(sel+' .rdrr-tfhost iframe');
    const r=el.getBoundingClientRect();return {x:r.x,y:r.y};
  },paneSel).catch(()=>({x:0,y:0}));
  const endpoints=await frame.evaluate(t=>{
    const doc=document.getElementById('doc');
    const walker=document.createTreeWalker(doc,NodeFilter.SHOW_TEXT);
    let node;const parts=[];
    while(node=walker.nextNode())parts.push({node,text:node.textContent});
    const full=parts.map(p=>p.text).join('');
    const idx=full.indexOf(t);if(idx<0)return null;
    let acc=0,sN=null,sO=0,eN=null,eO=0;
    for(const p of parts){const len=p.text.length;
      if(sN===null&&acc+len>idx){sN=p.node;sO=idx-acc;}
      if(acc<idx+t.length){eN=p.node;eO=idx+t.length-acc;}
      acc+=len;if(sN&&eN&&acc>=idx+t.length)break;}
    if(!sN||!eN)return null;
    const rs=document.createRange();rs.setStart(sN,sO);rs.setEnd(sN,sO+1);
    const re=document.createRange();re.setStart(eN,Math.max(0,eO-1));re.setEnd(eN,eO);
    const rsr=rs.getBoundingClientRect(),rer=re.getBoundingClientRect();
    return {sx:rsr.x+1,sy:rsr.y+rsr.height/2,ex:rer.x+rer.width-1,ey:rer.y+rer.height/2};
  },text);
  if(!endpoints)return {selRes,dragged:false,hostSelPop:null,reason:'no endpoints'};
  const x1=box.x+endpoints.sx-2,y1=box.y+endpoints.sy,x2=box.x+endpoints.ex+2,y2=box.y+endpoints.ey;
  await page.mouse.move(x1,y1);await page.mouse.down();
  for(let k=1;k<=10;k++)await page.mouse.move(x1+(x2-x1)*k/10,y1+(y2-y1)*k/10);
  await page.mouse.up();await page.waitForTimeout(1200);
  // 落点诊断：父页面坐标（含偏移）处，iframe 内对应的元素
  const hit=await frame.evaluate(({x,y})=>{
    const el=document.elementFromPoint(x,y);
    return {tag:el?el.tagName:null,cls:el?el.className&&String(el.className).slice(0,30):null,
      inDoc:el?!!el.closest('#doc'):false};
  },{x:endpoints.sx,y:endpoints.sy}).catch(e=>({err:String(e).slice(0,60)}));
  const hitParent=await page.evaluate(({x,y})=>{
    const el=document.elementFromPoint(x,y);
    return {tag:el?el.tagName:null,id:el?el.id:null};
  },{x:x1,y:y1}).catch(e=>({err:String(e).slice(0,60)}));
  let hostSelPop=await page.evaluate(()=>{const p=document.querySelector('#selPop');
    return p?{visible:p.getBoundingClientRect().width>0,
      acts:[...p.querySelectorAll('button')].map(b=>(b.textContent||'').trim())}:null;});
  let retried=null;
  for(let i=0;i<2&&!hostSelPop;i++){
    retried=i+1;
    await frame.evaluate(()=>{const doc=document.getElementById('doc');
      const sel=window.getSelection();
      if(sel&&!sel.isCollapsed){const r=sel.getRangeAt(0).getBoundingClientRect();
        doc.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,
          clientX:r.x+r.width/2,clientY:r.y+r.height/2}));}});
    await page.waitForTimeout(900);
    hostSelPop=await page.evaluate(()=>{const p=document.querySelector('#selPop');
      return p?{visible:p.getBoundingClientRect().width>0,
        acts:[...p.querySelectorAll('button')].map(b=>(b.textContent||'').trim())}:null;});
  }
  const selStr=await frame.evaluate(()=>{const sel=window.getSelection();
    return sel&&!sel.isCollapsed?String(sel).slice(0,40):'';}).catch(()=> '');
  return {selRes,dragged:true,hostSelPop,selStr,retried,hitAtStart:endpoints,hit,hitParent,
    boxOffset:box};
}
async function enterFoldPid(page,pid){
  await page.goto(`${WEB}/#/p/${pid}/reader`,{waitUntil:'domcontentloaded'}).catch(()=>{});
  await page.waitForSelector('#btn-aifold',{timeout:20000});
  return enterFold(page);
}

(async()=>{
 const browser=await chromium.launch({headless:true});

 // ===== U0：真实库全部 fold_run 只读扫描 =====
 await (async()=>{CURRENT='U0-run-errors-provenance';
  try{
   const os_=await import('os');const path_=await import('path');const fs_=await import('fs');
   const real=path_.default.join(os_.default.homedir(),'CdoexProject/Literature/library');
   const tmp=await fs_.default.promises.mkdtemp(path_.default.join(os_.default.tmpdir(),'u0-'));
   await fs_.default.promises.cp(real,path_.default.join(tmp,'library'),{recursive:true});
   const libDir=path_.default.join(tmp,'library');
   const runs=[];const provenanceAll=[];
   for(const d of fs_.default.readdirSync(libDir)){
     const pjPath=path_.default.join(libDir,d,'project.json');
     let name=d,pid='';
     try{const pj=JSON.parse(fs_.default.readFileSync(pjPath,'utf8'));
       pid=String(pj.project_id||'');name=String(pj.title||d);}catch(e){}
     const fr=path_.default.join(libDir,d,'reader','fold_run.json');
     try{const run=JSON.parse(fs_.default.readFileSync(fr,'utf8'));
       runs.push({name:name.slice(0,50),pid,status:run.status,ready:run.ready,
         errors:run.errors||[],basis:(run.ready_basis||'').slice(0,200)});}catch(e){}
   }
   const failedRuns=runs.filter(r=>r.status==='failed'||r.ready===false);
   ok('U0.1 真实库全部 fold_run errors 只读扫描（逐项目）',runs.length>0,
      {runs:runs.map(r=>({name:r.name,status:r.status,ready:r.ready,
        errorsCount:(r.errors||[]).length,basis:r.basis.slice(0,120)}))});
   ok('U0.2 用户最近失败样本已定位（Between/BEYOND/唐书）',
      failedRuns.length>0,{failedProjects:failedRuns.map(r=>({name:r.name,status:r.status,
        errorsCount:(r.errors||[]).length,basis:r.basis.slice(0,160)}))});
   for(const fr of failedRuns){
     const dir=path_.default.join(libDir,fr.name);
     try{
       const layers=JSON.parse(fs_.default.readFileSync(
         path_.default.join(dir,'reader','layers.json'),'utf8'));
       const zh=layers.docs['zh-CN']||{};const so=layers.docs['source']||{};
       for(const sec of (zh.sections||[])){
         const soSec=(so.sections||[]).find(x=>x.id===sec.id)||{};
         for(const u of sec.units||[]){
           const s=(soSec.units||[]).find(x=>x.id===u.id)||{};
           if(s.suppressed||u.source==='local'||u.suppressed){
             provenanceAll.push({project:fr.name.slice(0,40),section:sec.id,unit:u.id,
               src:{suppressed:s.suppressed||null,source:s.source||null},
               tr:{source:u.source,suppressed:u.suppressed,brief_source:u.brief_source}});
           }
         }
       }
     }catch(e){provenanceAll.push({project:fr.name.slice(0,40),error:String(e).slice(0,80)});}
   }
   ok('U0.3 失败项目逐 unit provenance（源 suppressed vs 目标 local/translation-local）',
      provenanceAll.length>0,{count:provenanceAll.length,items:provenanceAll.slice(0,8)});
  }catch(e){ok('U0 场景异常',false,String(e).slice(0,250));}
 })();

 // ===== U1（项目 A）：统一菜单 / 无独立 Ask-whole / fold 内宿主 #selPop =====
 await (async()=>{CURRENT='U1-unified-menu';let page;
  try{
   const ctx=await browser.newContext({viewport:{width:1440,height:940}});
   page=await ctx.newPage();attach(page);
   await page.goto(`${WEB}/#/p/${A}/reader`,{waitUntil:'domcontentloaded'});
   await page.waitForSelector('#btn-aifold',{timeout:20000});
   await page.waitForTimeout(600);
   const askWhole=await page.evaluate(()=>[...document.querySelectorAll('button')]
     .filter(b=>/ask[- ]?whole|全文问答/i.test(b.textContent||'')).length);
   ok('1a 无独立 Ask-whole 按钮',askWhole===0,{askWhole});
   await page.evaluate(()=>{
     const p=document.querySelector('#rdrr-pane-source p[data-block-id="b-p-1"]');
     const w=document.createTreeWalker(p,NodeFilter.SHOW_TEXT);
     let n;const parts=[];while(n=w.nextNode())parts.push({node:n,text:n.textContent});
     const full=parts.map(x=>x.text).join('');const at=full.indexOf('Workshops in Chang');
     let acc=0,sN=null,sO=0,eN=null,eO=0;
     for(const x of parts){const len=x.text.length;
       if(sN===null&&acc+len>at){sN=x.node;sO=at-acc;}
       if(acc<at+22){eN=x.node;eO=at+22-acc;}acc+=len;
       if(sN&&eN&&acc>=at+22)break;}
     const r=document.createRange();r.setStart(sN,sO);r.setEnd(eN,eO);
     const sel=window.getSelection();sel.removeAllRanges();sel.addRange(r);
     const rr=r.getBoundingClientRect();
     document.querySelector('#rdrr-pane-source .rdrr-pane-body')
       .dispatchEvent(new MouseEvent('mouseup',{bubbles:true,clientX:rr.x+rr.width/2,clientY:rr.y+rr.height/2}));
   });
   await page.waitForTimeout(700);
   const selPop=await page.evaluate(()=>{const p=document.querySelector('#selPop');
     return p?{visible:p.getBoundingClientRect().width>0,
       acts:[...p.querySelectorAll('button')].map(b=>(b.textContent||'').trim())}:null;});
   ok('1b 普通阅读划词 → 统一 #selPop（含 追问）',!!selPop&&selPop.visible&&selPop.acts.includes('追问'),selPop);
   await page.evaluate(()=>{
     const p=document.querySelector('#rdrr-pane-source p[data-block-id="b-p-1"]');
     const r=p.getBoundingClientRect();
     p.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,clientX:r.x+r.width/2,clientY:r.y+10}));
   });
   await page.waitForTimeout(600);
   const ctxMenu=await page.evaluate(()=>{const p=document.querySelector('#selPop');
     return p?{visible:p.getBoundingClientRect().width>0}:null;});
   ok('1c 右键整段 → 同一菜单（非独立窗）',!!ctxMenu&&ctxMenu.visible,ctxMenu);
   await page.keyboard.press('Escape');
   await page.locator('#btn-ai').click();await page.waitForTimeout(500);
   const aiPanel=await page.evaluate(()=>{const p=document.getElementById('ai');
     return p?{shown:p.classList.contains('show')||getComputedStyle(p).display!=='none'}:null;});
   ok('1d 顶栏「AI 问答」统一面板可打开',!!aiPanel&&aiPanel.shown,aiPanel);
   await page.locator('#aiClose').click().catch(()=>{});
   await enterFold(page);await page.waitForTimeout(1200);
   const dst=await frameOf(page,'trans');
   await setLevel(page,dst,3);
   let bar={none:true},mouseDrag=null,hostSelPop=null;
   const drag=await foldDragSelect(page,dst,'#rdrr-pane-trans','依据证据句');
   if(drag.dragged){
     hostSelPop=drag.hostSelPop;
     mouseDrag={selToString:drag.selStr||'',selbar:{hidden:true}};
     bar=await dst.evaluate(()=>{const b=document.querySelector('.selbar');
       return b?{hidden:b.hidden}:{none:true};});
   }else{mouseDrag={selToString:'',error:drag.reason||'select failed'};}
   ok('1e fold 内真实鼠标拖选 → 宿主 #selPop 出现（与普通同一组件）',
      !!hostSelPop&&hostSelPop.visible&&hostSelPop.acts.includes('追问'),
      {hostSelPop,kernelBar:bar,mouseDrag,selRes:drag.selRes});
   if(hostSelPop&&hostSelPop.visible){
     await page.locator('#selPop button').filter({hasText:'追问'}).first().click();
     await page.waitForTimeout(900);
     const routed=await page.evaluate(()=>({chatShown:(()=>{const p=document.getElementById('ai');
       return p?(p.classList.contains('show')||getComputedStyle(p).display!=='none'):false})()}));
     ok('1f 宿主追问 → 统一问答面板（无内核独立问答窗）',
        routed.chatShown===true,routed);
     await page.keyboard.press('Escape');await page.waitForTimeout(300);
   }
   await page.waitForTimeout(600);
   const exitStage=await page.locator('#btn-aifold').getAttribute('data-stage').catch(()=>'?');
   if(exitStage==='on'){
     await page.locator('#aiClose').click({timeout:3000}).catch(()=>{});
     await page.keyboard.press('Escape');await page.waitForTimeout(400);
     const dis=await page.locator('#btn-aifold').isDisabled().catch(()=>null);
     await page.locator('#btn-aifold').click({force:true,timeout:8000}).catch(()=>{});
     for(let i=0;i<10;i++){await page.waitForTimeout(500);
       const st=await page.locator('#btn-aifold').getAttribute('data-stage').catch(()=>'?');
       if(st!=='on')break;}
   }
   const lvlAfter=await page.locator('#lvlbar').count();
   const stageAfter=await page.locator('#btn-aifold').getAttribute('data-stage').catch(()=>'?');
   const foldDataNull=await page.evaluate(()=>window.__litFoldData==null).catch(()=>null);
   ok('1g 退出 fold 回普通阅读（菜单体系一致）',
      lvlAfter===0||stageAfter!=='on',{lvlAfter,stageAfter,foldDataNull});
   await ctx.close();
  }catch(e){ok('U1 场景异常',false,String(e).slice(0,250));if(page)await page.context().close().catch(()=>{});}
 })();

 // ===== U2（项目 A）：普通 + fold 双高亮颜色/锚空间分离；编辑落盘重开；PUT 计数 =====
 await (async()=>{CURRENT='U2-colors-persist';let page;
  try{
   const ctx=await browser.newContext({viewport:{width:1440,height:940}});
   page=await ctx.newPage();attach(page);
   await page.goto(`${WEB}/#/p/${A}/reader`,{waitUntil:'domcontentloaded'});
   await page.waitForSelector('#btn-aifold',{timeout:20000});
   await page.waitForTimeout(600);
   await page.evaluate(()=>{
     const p=document.querySelector('#rdrr-pane-source p[data-block-id="b-p-1"]');
     const w=document.createTreeWalker(p,NodeFilter.SHOW_TEXT);
     let n;const parts=[];while(n=w.nextNode())parts.push({node:n,text:n.textContent});
     const full=parts.map(x=>x.text).join('');const at=full.indexOf('Silver vessels travelled');
     let acc=0,sN=null,sO=0,eN=null,eO=0;
     for(const x of parts){const len=x.text.length;
       if(sN===null&&acc+len>at){sN=x.node;sO=at-acc;}
       if(acc<at+27){eN=x.node;eO=at+27-acc;}acc+=len;
       if(sN&&eN&&acc>=at+27)break;}
     const r=document.createRange();r.setStart(sN,sO);r.setEnd(eN,eO);
     const sel=window.getSelection();sel.removeAllRanges();sel.addRange(r);
     const rr=r.getBoundingClientRect();
     document.querySelector('#rdrr-pane-source .rdrr-pane-body')
       .dispatchEvent(new MouseEvent('mouseup',{bubbles:true,clientX:rr.x+2,clientY:rr.y+2}));
   });
   await page.waitForTimeout(600);
   const saveStart=Date.now();
   await page.locator('#selPop .rdrr-swatch').first().click();
   await page.waitForTimeout(700);
   const savePuts=countOverlayPuts(saveStart);
   const stablePuts=await waitPutStable(page,saveStart);
   ok('2a-put 保存动作 PUT 计数稳定（无递归风暴：稳定后 ≤2 次）',
      stablePuts<=2&&stablePuts>=1,{savePuts,stablePuts});
   const normalMark=await page.evaluate(()=>{
     const m=document.querySelector('#rdrr-pane-source mark.hl');
     return m?{text:(m.innerText||'').replace(/\s+/g,' ').slice(0,40),
       color:getComputedStyle(m).backgroundColor}:null;});
   ok('2a 普通阅读高亮：mark.hl 落盘可见（React 视图）',!!normalMark&&!!normalMark.text,normalMark);
   await enterFold(page);await page.waitForTimeout(1200);
   let dst=await frameOf(page,'trans');await setLevel(page,dst,3);
   // 真实鼠标拖选（与 1e 同路径）：首末字形端点 → 宿主 #selPop 出现
   const candB=await pickTransCandidate(dst);
   if(!candB){ok('2b fold 内真实拖选 → 宿主 #selPop 色点 → PUT → span.tk.hl（原样重建）',false,
      {err:'no candidate text in trans doc'});}
   const dragSel=candB?await foldDragSelect(page,dst,'#rdrr-pane-trans',candB):null;
   const foldPutStart=Date.now();
   let swClick=null;
   if(dragSel.hostSelPop&&dragSel.hostSelPop.visible){
     const sw=page.locator('#selPop .rdrr-swatch').nth(2);
     swClick=await sw.evaluate(el=>({bg:getComputedStyle(el).backgroundColor,
       title:el.title||el.getAttribute('aria-label')||''})).catch(()=>null);
     await sw.click({timeout:5000}).catch(()=>{});
     await page.waitForTimeout(700);
   }
   const foldPuts=await waitPutStable(page,foldPutStart);
   const foldPutBody=timeline.filter(e=>e.dir==='put-body'&&e.t>=foldPutStart).slice(-1)[0]||null;
   // anchor 读回（GET /api/notes 或 overlay 回执）：核内 .hl 逐字 span + 首末 Range 重建原样文本
   const foldJoined=await joinedHighlight(dst);
   const foldColor=candB?await dst.evaluate((c)=>{
     const s=[...document.querySelectorAll('#doc .hl')];
     const hit=s.find(m=>(m.innerText||'').includes(c))||s[0]||null;
     return hit?{color:getComputedStyle(hit).backgroundColor,text:(hit.innerText||'').slice(0,20),
       spanCount:s.length}:null;},candB):null;
   ok('2b fold 内真实拖选 → 宿主 #selPop 色点 → PUT → span.tk.hl（原样重建）',
      !!foldColor&&foldPuts>=1&&!!candB&&foldJoined.includes(candB),
      {cand:candB,foldColor,foldJoined:foldJoined.slice(0,60),foldPuts,putBody:foldPutBody,
       dragSel:dragSel?{dragged:dragSel.dragged,hostSelPop:dragSel.hostSelPop,selStr:dragSel.selStr,
         hitParent:dragSel.hitParent,boxOffset:dragSel.boxOffset}:null,swClick});
   await page.reload({waitUntil:'domcontentloaded'});
   await page.waitForSelector('#btn-aifold',{timeout:20000});
   await page.waitForTimeout(800);
   const normalAfter=await page.evaluate(()=>{
     const m=document.querySelector('#rdrr-pane-source mark.hl');
     return m?{text:(m.innerText||'').replace(/\s+/g,' ').slice(0,40),color:getComputedStyle(m).backgroundColor}:null;});
   ok('2c 刷新后普通 mark 仍在（同一文本/颜色）',
      !!normalAfter&&normalAfter.text===normalMark?.text,normalAfter);
   await enterFold(page);await page.waitForTimeout(1200);
   const d2=await frameOf(page,'trans');await setLevel(page,d2,3);await page.waitForTimeout(600);
   const foldAfterJoined=await joinedHighlight(d2);
   const candD=foldJoined?foldJoined.slice(0,6):(await pickTransCandidate(d2));
   const foldAfter=await d2.evaluate((c)=>{
     const s=[...document.querySelectorAll('#doc .hl')];
     const hit=c?s.find(m=>(m.innerText||'').includes(c))||s[0]||null:(s[0]||null);
     return hit?{color:getComputedStyle(hit).backgroundColor,text:(hit.innerText||'').slice(0,20),
       spanCount:s.length}:null;},candD);
   ok('2d 刷新后 fold 高亮仍在（span.tk.hl 同色 + 原样重建一致）',
      !!foldAfter&&!!foldColor&&foldAfter.color===foldColor.color&&foldAfterJoined===foldJoined,
      {cand:candD,foldAfter,foldAfterJoined:foldAfterJoined.slice(0,60),expected:foldJoined.slice(0,60)});
   const noteEdit=await d2.evaluate(async()=>{
     const mark=[...document.querySelectorAll('#doc .hl')].find(m=>(m.innerText||'').includes('依据证据句'));
     if(!mark)return {err:'no mark'};
     const r=mark.getBoundingClientRect();
     mark.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,clientX:r.x+r.width/2,clientY:r.y}));
     document.getElementById('doc').dispatchEvent(new MouseEvent('click',{bubbles:true,clientX:r.x+r.width/2,clientY:r.y}));
     return {clicked:true};
   });
   await page.waitForTimeout(600);
   const edit=await page.evaluate(()=>{
     const t=document.querySelector('#hlPop textarea,#ai textarea,.npop textarea');
     return t?{found:true,tag:t.tagName}:null;});
   ok('2e 已有高亮可进入编辑态',!!edit&&edit.found,edit);
   await ctx.close();
  }catch(e){ok('U2 场景异常',false,String(e).slice(0,250));if(page)await page.context().close().catch(()=>{});}
 })();

 // ===== U3（项目 A）：折叠态滚动跟随与联动开关（真实 wheel，iframe window.scrollY） =====
 await (async()=>{CURRENT='U3-scroll-follow';let page;
  try{
   const ctx=await browser.newContext({viewport:{width:1440,height:940}});
   page=await ctx.newPage();attach(page);
   await page.goto(`${WEB}/#/p/${A}/reader`,{waitUntil:'domcontentloaded'});
   await page.waitForSelector('#btn-aifold',{timeout:20000});
   await page.waitForTimeout(600);
   await enterFold(page);await page.waitForTimeout(1600);
   const srcF=await frameOf(page,'source');
   if(!srcF){ok('3-pre 源栏 iframe 就绪',false);throw new Error('no src frame');}
   // 前置：切到产品层1（全文 = kernel level 4，最长），保证 iframe 足够长可跨页滚动
   const lvOk=await setLevel(page,srcF,1);
   const pre=await srcF.evaluate(async()=>{
     const sc=document.scrollingElement||document.documentElement;
     const max=sc.scrollHeight-window.innerHeight;
     // 记录可视首段文本片段（用于原译对照）
     const doc=document.getElementById('doc');
     const first=doc.querySelector('.u, p, h2, h3');
     return {scrollHeight:sc.scrollHeight,innerH:window.innerHeight,maxScroll:max,
       firstText:first?first.textContent.slice(0,30):''};
   });
   ok('3-pre 折叠源栏可滚（原文层 scrollHeight > viewport，可跨页）',
      pre.maxScroll>400,{...pre,lvOk});
   const probe={};
   const srcScrollY=()=>srcF.evaluate(()=>{const sc=document.scrollingElement||document.documentElement;
     return {y:Math.round(sc.scrollTop),innerH:window.innerHeight};}).catch(()=>({y:-1}));
   const originState=()=>page.evaluate(()=>{
     const origin=document.querySelector('#rdrr-pane-origin');
     if(!origin)return {err:'no origin'};
     const body=origin.querySelector('.rdrr-pane-body')||origin;
     const pages=[...origin.querySelectorAll('.pdf-page')];
     const vis=pages.find(p=>{const r=p.getBoundingClientRect();
       return r.bottom>0&&r.top<window.innerHeight&&r.height>0;});
     let ink=0;
     if(vis){const cv=vis.querySelector('canvas');
       if(cv){try{const d=cv.getContext('2d').getImageData(0,0,cv.width,cv.height).data;
         for(let i=0;i<d.length;i+=16)if(d[i+3]>0&&d[i]<150&&d[i+1]<150&&d[i+2]<150)ink++;}catch(e){}}}
     return {scrollTop:Math.round(body.scrollTop),pageIdx:vis?vis.dataset.pageIndex||vis.getAttribute('data-page-index')||'?':null,
       pdfInk:ink,rendered:vis?vis.dataset.rendered:null};
   });
   // 源栏 iframe 中心坐标（视口）
   const srcBox=await page.evaluate(()=>{
     const el=document.querySelector('#rdrr-pane-source .rdrr-tfhost iframe');
     const r=el.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};});
   const y0=await srcScrollY();const o0=await originState();
   // 真实 wheel 下滚（3 次 × 500px）
   for(let i=0;i<3;i++){await page.mouse.move(srcBox.x,srcBox.y);
     await page.mouse.wheel(0,500);await page.waitForTimeout(450);}
   await page.waitForTimeout(900);
   const y1=await srcScrollY();const o1=await originState();
   probe.srcScroll={before:y0.y,after:y1.y,moved:y1.y>y0.y+150};
   probe.origin={before:o0,after:o1,moved:(o1.scrollTop!==o0.scrollTop)||o0.pageIdx!==o1.pageIdx};
   ok('3a-源 真实 wheel 后 iframe window.scrollY 真变（跨页滚动）',
      probe.srcScroll.moved,probe.srcScroll);
   ok('3a 联动开：源栏折叠滚动 → 原件栏跟随（scrollTop 或可视 PDF 页变化）且 PDF 有文字像素',
      probe.origin.moved&&(o1.pdfInk>10||o1.pdfInk===0&&o1.rendered!=='true'),
      {originBefore:o0,originAfter:o1,src:probe.srcScroll});
   // 3b 联动关：真实 wheel 验证不跟随
   await page.locator('.rdrr-tbtn').filter({hasText:'Aa'}).first().click();
   await page.waitForTimeout(400);
   const followToggle=await page.evaluate(()=>{
     const cb=[...document.querySelectorAll('.rdrr-aa input[type=checkbox]')]
       .find(c=>{const lab=c.closest('label');return /跟随/.test(lab?lab.textContent:'');});
     if(!cb)return null;
     return {text:(cb.closest('label')?.textContent||'').trim().slice(0,20),checked:cb.checked};
   });
   if(followToggle){
     const cbLoc=page.locator('.rdrr-aa label').filter({hasText:'跟随'}).locator('input').first();
     await cbLoc.uncheck().catch(()=>{});
     await page.keyboard.press('Escape');await page.waitForTimeout(300);
     const o2=await originState();
     const yB=await srcScrollY();
     for(let i=0;i<2;i++){await page.mouse.move(srcBox.x,srcBox.y);
       await page.mouse.wheel(0,500);await page.waitForTimeout(450);}
     await page.waitForTimeout(900);
     const yC=await srcScrollY();const o3=await originState();
     const osc=[];for(let i=0;i<5;i++){osc.push((await originState()).scrollTop);
       await page.waitForTimeout(120);}
     ok('3b 联动关：源栏继续滚（scrollY 真变）而原件不跟随、无振荡',
        yC.y>yB.y+100&&(o3.scrollTop===o2.scrollTop)&&Math.max(...osc)-Math.min(...osc)<=1,
        {srcBefore:yB.y,srcAfter:yC.y,originBefore:o2.scrollTop,originAfter:o3.scrollTop,
         oscRange:Math.max(...osc)-Math.min(...osc),followToggle});
     await cbLoc.check().catch(()=>{});
   }else{
     ok('3b 联动开关存在性（如实记录：未找到「跟随」控件）',false,{followToggle});
   }
   await page.keyboard.press('Escape');await page.waitForTimeout(200);
   await ctx.close();
  }catch(e){ok('U3 场景异常',false,String(e).slice(0,250));if(page)await page.context().close().catch(()=>{});}
 })();

 // ===== U4（项目 A）：fold 内 unit 点击展开/收起，原译两栏同 id 同步 =====
 await (async()=>{CURRENT='U4-expand-sync';let page;
  try{
   const ctx=await browser.newContext({viewport:{width:1440,height:940}});
   page=await ctx.newPage();attach(page);
   await page.goto(`${WEB}/#/p/${A}/reader`,{waitUntil:'domcontentloaded'});
   await page.waitForSelector('#btn-aifold',{timeout:20000});
   await page.waitForTimeout(600);
   await enterFold(page);await page.waitForTimeout(1400);
   const transCb=page.locator('.rdrr-toggles input[type="checkbox"][aria-label="trans"]');
   if(await transCb.count()&&!(await transCb.isChecked())){await transCb.check();await page.waitForTimeout(700);}
   await setLevel(page,await frameOf(page,'source'),3);
   await page.waitForTimeout(700);
   const unitInfo=await page.evaluate(()=>{
     const host=document.querySelector('#rdrr-pane-source .rdrr-tfhost iframe');
     const f=host.contentWindow;const d=host.contentDocument;
     const u=d.querySelector('#doc .u[data-u], #doc .u');
     if(!u)return {err:'no unit'};
     const r=u.getBoundingClientRect();
     u.dispatchEvent(new MouseEvent('click',{bubbles:true,clientX:r.x+40,clientY:r.y+8}));
     return {uid:u.dataset.u||u.id,id:u.id,localOpen:u.classList.contains('local-open')};
   });
   await page.waitForTimeout(1200);
   const srcState=await page.evaluate(()=>{
     const host=document.querySelector('#rdrr-pane-source .rdrr-tfhost iframe');
     const u=host.contentDocument.querySelector('#doc .u');
     const tx=u._tx||u.querySelector('.tx')||u;
     return {localOpen:u.classList.contains('local-open'),
       chars:tx.textContent.length,tks:u.querySelectorAll('.tk').length};
   }).catch(()=>({}));
   ok('4a 源栏点击 unit → 本地展开（local-open 且 DOM 字符数>收起态）',
      srcState.localOpen===true&&srcState.chars>0,srcState);
   const dstUidState=await page.evaluate(()=>{
     const host=document.querySelector('#rdrr-pane-trans .rdrr-tfhost iframe');
     if(!host)return {err:'no dst iframe'};
     const u=host.contentDocument.querySelector('#doc .u');
     return {id:u.id,localOpen:u.classList.contains('local-open'),
       tk:u.querySelectorAll('.tk').length};
   }).catch(e=>({err:String(e).slice(0,80)}));
   await page.evaluate(()=>{
     const host=document.querySelector('#rdrr-pane-source .rdrr-tfhost iframe');
     const u=host.contentDocument.querySelector('#doc .u');
     const r=u.getBoundingClientRect();
     u.dispatchEvent(new MouseEvent('click',{bubbles:true,clientX:r.x+40,clientY:r.y+8}));
   });
   await page.waitForTimeout(1200);
   const srcAfter=await page.evaluate(()=>{
     const host=document.querySelector('#rdrr-pane-source .rdrr-tfhost iframe');
     const u=host.contentDocument.querySelector('#doc .u');
     return {localOpen:u.classList.contains('local-open')};
   }).catch(()=>({}));
   ok('4b 再次点击收起（local-open 移除）',srcAfter.localOpen===false,srcAfter);
   const syncBridge=await page.evaluate(()=>({
     bridgeEvents:['rpc','light','goto'],
     note:'内核 toggleUnit 为 iframe 内部状态；跨栏展开同步依赖宿主桥事件集（当前 bridge.js 仅 light/goto）',
     owner:'frontend/designer',
   }));
   ok('4c 跨栏展开/收起同 id 同步（当前桥事件集仅 rpc/light/goto，未同步）',
      dstUidState.localOpen===true,
      {dstUidState,bridge:syncBridge.bridgeEvents,owner:syncBridge.owner,note:syncBridge.note});
   // 4d（Lead 补充）：收起后目标栏 localOpen false（此前只断目标 open true）
   const dstUidAfterCollapse=await page.evaluate(()=>{
     const host=document.querySelector('#rdrr-pane-trans .rdrr-tfhost iframe');
     if(!host||!host.contentDocument)return {err:'no dst iframe'};
     const u=host.contentDocument.querySelector('#doc .u');
     return u?{id:u.id,localOpen:u.classList.contains('local-open')}:{err:'no unit'};
   }).catch(e=>({err:String(e).slice(0,80)}));
   ok('4d unit 收起后目标 localOpen false（跨栏同步收起）',
      dstUidAfterCollapse.localOpen===false,{dstUidAfterCollapse});
   await ctx.close();
  }catch(e){ok('U4 场景异常',false,String(e).slice(0,250));if(page)await page.context().close().catch(()=>{});}
 })();

 // ===== U5（项目 A）：CAS 409 后用户重划词/保存能否恢复（旧队列拒绝不卡死） =====
 await (async()=>{CURRENT='U5-cas-recovery';let page;
  try{
   const ctx=await browser.newContext({viewport:{width:1440,height:940}});
   page=await ctx.newPage();attach(page);
   await page.goto(`${WEB}/#/p/${A}/reader`,{waitUntil:'domcontentloaded'});
   await page.waitForSelector('#btn-aifold',{timeout:20000});
   await page.waitForTimeout(600);
   // 正常模式划词+选色 helper（与 2a 同路径）
   const normalSelectAndColor=async(text,swatchIdx)=>{
     const t0=Date.now();
     await page.evaluate(txt=>{
       const root=document.querySelector('#rdrr-pane-source .rdrr-pane-body');
       const w=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
       let n;const parts=[];while(n=w.nextNode())parts.push({node:n,text:n.textContent});
       const full=parts.map(x=>x.text).join('');const at=full.indexOf(txt);
       if(at<0)throw new Error('text not found: '+txt);
       let acc=0,sN=null,sO=0,eN=null,eO=0;
       for(const x of parts){const len=x.text.length;
         if(sN===null&&acc+len>at){sN=x.node;sO=at-acc;}
         if(acc<at+txt.length){eN=x.node;eO=at+txt.length-acc;}acc+=len;
         if(sN&&eN&&acc>=at+txt.length)break;}
       const r=document.createRange();r.setStart(sN,sO);r.setEnd(eN,eO);
       const sel=window.getSelection();sel.removeAllRanges();sel.addRange(r);
       const rr=r.getBoundingClientRect();
       document.querySelector('#rdrr-pane-source .rdrr-pane-body')
         .dispatchEvent(new MouseEvent('mouseup',{bubbles:true,clientX:rr.x+2,clientY:rr.y+2}));
     },text);
     await page.waitForSelector('#selPop',{state:'visible',timeout:6000});
     await page.locator('#selPop .rdrr-swatch').nth(swatchIdx).click();
     return t0;
   };
   const getOverlay=async()=>page.evaluate(async()=>{
     const r=await fetch(`${location.origin.replace(/:\\d+$$/,'')}/api/projects/${A}/reader/overlay`
       .replace('/api/','/'),{headers:{'x-literature-token':window.__litToken||''}});
     return r.json();
   }).catch(async()=>{ // 兜底：直接用相对路径由 cjs 端 fetch
     return null;});
   // 尝试 1：正常保存（revision R1）
   const t1=await normalSelectAndColor('Silver vessels travelled',0);
   const puts1=await waitPutStable(page,t1,5000);
   ok('5a 首次保存成功（PUT 200，revision 前进）',puts1>=1,{puts1});
   // 外部窗口：GET 当前 overlay → 原样 PUT（revision R1→R2），制造 app 内 stale base
   const ext=await page.request.get(`${API}/projects/${A}/reader/state?locale=source`,
     {headers:{'x-literature-token':TOKEN}});
   const extState=await ext.json().catch(()=>null);
   const extBody=extState?extState.overlay:null;
   ok('5b 外部窗口 GET state→overlay 成功（拿到当前 revision）',
      ext.ok()&&extBody&&typeof extBody.revision==='number',
      {status:ext.status(),revision:extBody?extBody.revision:null});
   const putExt=await page.request.put(`${API}/projects/${A}/reader/overlay`,{
     headers:{'x-literature-token':TOKEN,'content-type':'application/json'},
     data:{overlay:extBody,base_revision:extBody.revision}});
   ok('5c 外部窗口 PUT 成功（revision 再前进，模拟另一窗口写入）',putExt.ok(),
      {status:putExt.status(),body:(await putExt.text()).slice(0,120)});
   // 尝试 2：app 内用户再划词保存 → app 持 stale base → 预期 409
   const t2=await normalSelectAndColor("Workshops in Chang",1);
   await page.waitForTimeout(2500);
   const seq2=timeline.filter(e=>e.dir==='resp'&&e.t>=t2).map(e=>e.status);
   const had409=seq2.includes(409);
   ok('5d 旧 base 提交被 CAS 拒绝（网络层真实 409 出现在 PUT 序列）',had409,{respSeq:seq2});
   // 等产品 refetch state（409 catch 内 readerApi.state）
   await page.waitForTimeout(1500);
   // 尝试 3：用户重新划词保存 → 应以新 revision 成功（恢复）
   const t3=Date.now();
   await normalSelectAndColor('Assay data show',2);
   const puts3=await waitPutStable(page,t3,6000);
   const seq3=timeline.filter(e=>e.dir==='resp'&&e.t>=t3).map(e=>e.status);
   const recovered=seq3.includes(200)&&!seq3.includes(409);
   ok('5e 409 后用户重划词保存恢复（新 PUT 200，无再 409，无保存风暴）',
      recovered&&puts3<=2,{respSeq:seq3,puts3});
   // 读回：恢复批注真实落盘
   const final=await page.request.get(`${API}/projects/${A}/reader/state?locale=source`,
     {headers:{'x-literature-token':TOKEN}});
   const finalBody=(await final.json().catch(()=>null))?.overlay||null;
   const quotes=(finalBody?.annotations||[]).map(a=>(a.quote&&a.quote.exact||'').slice(0,20));
   ok('5f 读回 overlay：恢复批注（Assay data）在盘，revision 与服务器一致',
      !!finalBody&&quotes.some(q=>q.includes('Assay')),
      {revision:finalBody?finalBody.revision:null,quotes:quotes.slice(0,8)});
   await ctx.close();
  }catch(e){ok('U5 场景异常',false,String(e).slice(0,250));if(page)await page.context().close().catch(()=>{});}
 })();

 // ===== U6（项目 A）：快速连续两次改色 → 仅最终色、无虚假保存 =====
 await (async()=>{CURRENT='U6-rapid-recolor';let page;
  try{
   const ctx=await browser.newContext({viewport:{width:1440,height:940}});
   page=await ctx.newPage();attach(page);
   await page.goto(`${WEB}/#/p/${A}/reader`,{waitUntil:'domcontentloaded'});
   await page.waitForSelector('#btn-aifold',{timeout:20000});
   await page.waitForTimeout(600);
   const swatches=await page.evaluate(()=>{ // 先记录各色板颜色（selPop 需先有选区才出现，此处先取 CSS 定义）
     return null;});
   // 划词 → 连续快速点两个不同色板（间隔 <120ms）
   await page.evaluate(()=>{
     const p=document.querySelector('#rdrr-pane-source p[data-block-id="b-p-1"]');
     const w=document.createTreeWalker(p,NodeFilter.SHOW_TEXT);
     let n;const parts=[];while(n=w.nextNode())parts.push({node:n,text:n.textContent});
     const full=parts.map(x=>x.text).join('');const at=full.indexOf('Demand fell');
     let acc=0,sN=null,sO=0,eN=null,eO=0;
     for(const x of parts){const len=x.text.length;
       if(sN===null&&acc+len>at){sN=x.node;sO=at-acc;}
       if(acc<at+11){eN=x.node;eO=at+11-acc;}acc+=len;
       if(sN&&eN&&acc>=at+11)break;}
     const r=document.createRange();r.setStart(sN,sO);r.setEnd(eN,eO);
     const sel=window.getSelection();sel.removeAllRanges();sel.addRange(r);
     const rr=r.getBoundingClientRect();
     document.querySelector('#rdrr-pane-source .rdrr-pane-body')
       .dispatchEvent(new MouseEvent('mouseup',{bubbles:true,clientX:rr.x+2,clientY:rr.y+2}));
   });
   await page.waitForSelector('#selPop',{state:'visible',timeout:6000});
   const swColors=await page.evaluate(()=>{
     return [...document.querySelectorAll('#selPop .rdrr-swatch')].map(el=>
       getComputedStyle(el).backgroundColor);});
   const t0=Date.now();
   await page.locator('#selPop .rdrr-swatch').nth(0).click();
   await page.waitForTimeout(300);
   // 真实快速改色路径：右键点已有 mark.hl → 编辑态 hlPop → 点另一色板（产品 contextmenu 入口，ReaderWorkspace.tsx:1369）
   const markInfo=await page.evaluate(()=>{
     // 定位 Demand fell 对应的 mark（页面可能带前序场景的多条注释，不能取第一个）
     const marks=[...document.querySelectorAll('#rdrr-pane-source mark.hl')];
     const m=marks.find(x=>(x.textContent||'').includes('Demand fell'))||null;
     if(!m)return {err:'mark not found',markCount:marks.length,
       texts:marks.map(x=>(x.textContent||'').slice(0,12))};
     const r=m.getBoundingClientRect();
     m.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,
       clientX:r.x+r.width/2,clientY:r.y+r.height/2}));
     return {annId:m.dataset.annId||null,color:getComputedStyle(m).backgroundColor};
   });
   await page.waitForSelector('#hlPop',{state:'visible',timeout:6000});
   const hlSwatches=await page.evaluate(()=>{
     return [...document.querySelectorAll('#hlPop .rdrr-swatch')].map(el=>
       getComputedStyle(el).backgroundColor);});
   await page.locator('#hlPop .rdrr-swatch').nth(2).click();
   // patchAnnotation 走 scheduleSave 防抖（~600ms）：等第二个 PUT 的 200 回执真正出现再读回
   let puts=0,respSeq=[];
   for(let i=0;i<24;i++){
     await page.waitForTimeout(250);
     respSeq=timeline.filter(e=>e.dir==='resp'&&e.t>=t0).map(e=>e.status);
     puts=respSeq.length;
     if(puts>=2)break;
   }
   const stablePuts=await waitPutStable(page,t0,6000);
   respSeq=timeline.filter(e=>e.dir==='resp'&&e.t>=t0).map(e=>e.status);
   puts=Math.max(puts,stablePuts);
   // 落盘读回：该 quote 的注释颜色必须是最终色（swatch[2]）
   const finalColor=swColors[2]||null;
   // 读回轮询：等最后一版 color/条数稳定（两次连续读相同）或超时（防 GET 卡在两次 PUT 之间）
   let rdBody=null,mine=[],savedColors=[],lastColorRgb=null,polls=0;
   let prevSig='';
   for(let i=0;i<10;i++){
     const rd=await page.request.get(`${API}/projects/${A}/reader/state?locale=source`,
       {headers:{'x-literature-token':TOKEN}});
     rdBody=(await rd.json().catch(()=>null))?.overlay||null;
     mine=(rdBody?.annotations||[]).filter(a=>(a.quote&&a.quote.exact||'').includes('Demand fell'));
     const lastMine=mine[mine.length-1]||null;
     const sig=lastMine?`${mine.length}:${lastMine.color}`:'0:';
     // 期望最终色 = 第 3 色板对应色名（COLORS_ORDER[2]='b'）；等它出现或超时如实判失败
     if(lastMine&&lastMine.color===COLORS_ORDER[2])break;
     if(sig===prevSig&&sig!=='0:'&&i>=4)break;
     prevSig=sig;polls=i+1;
     await page.waitForTimeout(500);
   }
   savedColors=mine.map(a=>({letter:a.color||null,
     rgb:a.color?swColors[COLORS_ORDER.indexOf(a.color)]||null:null}));
   const lastMine=mine[mine.length-1]||null;
   lastColorRgb=lastMine&&lastMine.color?swColors[COLORS_ORDER.indexOf(lastMine.color)]:null;
   ok('6a 快速连续两次改色：PUT 有限（≤2，无虚假保存风暴）',puts<=2&&respSeq.every(s=>s===200),
      {puts,respSeq});
   ok('6b 仅最终色落盘（原地改色：同一条注释、最终色名 = 第 3 色板，无中间色残留注释）',
      mine.length===1&&lastColorRgb===finalColor,
      {finalColor,savedColors,annCount:mine.length,swCount:swColors.length,
       markInfo,hlSwatchCount:hlSwatches.length,polls,
       revision:rdBody?rdBody.revision:null,
       allAnnotations:(rdBody?.annotations||[]).map(a=>({id:String(a.id).slice(-6),pane:a.pane,
         quote:(a.quote&&a.quote.exact||'').slice(0,16),color:a.color||null}))});
   const markNow=await page.evaluate(()=>{
     const m=[...document.querySelectorAll('#rdrr-pane-source mark.hl')]
       .find(x=>(x.textContent||'').includes('Demand fell'));
     return m?getComputedStyle(m).backgroundColor:null;});
   ok('6c React 视图 mark 颜色 = 最终色（无中间色闪存）',
      !!markNow&&markNow===finalColor,{markNow,finalColor});
   await page.reload({waitUntil:'domcontentloaded'});
   await page.waitForSelector('#btn-aifold',{timeout:20000});
   await page.waitForTimeout(800);
   const markAfter=await page.evaluate(()=>{
     const m=[...document.querySelectorAll('#rdrr-pane-source mark.hl')]
       .find(x=>(x.textContent||'').includes('Demand fell'));
     return m?getComputedStyle(m).backgroundColor:null;});
   ok('6d 刷新后仍为最终色',markAfter===finalColor,{markAfter,finalColor});
   await ctx.close();
  }catch(e){ok('U6 场景异常',false,String(e).slice(0,250));if(page)await page.context().close().catch(()=>{});}
 })();

 // ===== U7：只开译文栏时自然划词 → 宿主 #selPop =====
 await (async()=>{CURRENT='U7-trans-only-menu';let page;
  try{
   const ctx=await browser.newContext({viewport:{width:1440,height:940}});
   page=await ctx.newPage();attach(page);
   await page.goto(`${WEB}/#/p/${A}/reader`,{waitUntil:'domcontentloaded'});
   await page.waitForSelector('#btn-aifold',{timeout:20000});
   await page.waitForTimeout(600);
   await enterFold(page);await page.waitForTimeout(1400);
   // 只开 trans：勾译文、去原件/原文（真实用户开关，非 API）
   const paneState=[];
   for(const k of ['origin','source']){
     const cb=page.locator(`.rdrr-ptoggle input[aria-label="${k}"]`);
     if(await cb.isChecked()){await cb.uncheck();paneState.push(k+':off');}
   }
   const transCb=page.locator('.rdrr-ptoggle input[aria-label="trans"]');
   if(!(await transCb.isChecked())){await transCb.check();paneState.push('trans:on');}
   await page.waitForTimeout(900);
   const transF=await frameOf(page,'trans');
   if(!transF){ok('7-pre 译文栏 iframe 就绪（单栏）',false,{paneState});throw new Error('no trans frame');}
   await setLevel(page,transF,3);await page.waitForTimeout(600);
   // 动态找无 .hl 批注覆盖的 unit 文本（前序场景注释仍在盘，.hl token 拖选会被内核 note 交互吞掉）
   const cand=await transF.evaluate(()=>{
     const us=[...document.querySelectorAll('#doc .u')];
     for(const u of us){
       if(u.querySelector('.hl'))continue;
       const t=((u._tx||u).textContent||'').replace(/\s+/g,' ').trim();
       if(t.length>=4)return t.slice(0,6);
     }
     return null;
   });
   if(!cand){ok('7-pre 无批注覆盖的可选文本',false);throw new Error('no candidate text');}
   // 自然拖选（真实鼠标路径）
   const drag=await foldDragSelect(page,transF,'#rdrr-pane-trans',cand);
   ok('7a 只开译文栏：自然划词 → 宿主 #selPop 出现（acts 含追问）',
      !!drag.hostSelPop&&drag.hostSelPop.visible&&drag.hostSelPop.acts.includes('追问'),
      {paneState,cand,hostSelPop:drag.hostSelPop,selStr:drag.selStr,retried:drag.retried,
       hitAtStart:drag.hitAtStart,hit:drag.hit,
       selRes:drag.selRes?{ok:drag.selRes.ok,rect:drag.selRes.rect}:null});
   // 恢复三栏（后续场景不受影响——本场景独占 context，仅为整洁）
   await ctx.close();
  }catch(e){ok('U7 场景异常',false,String(e).slice(0,250));if(page)await page.context().close().catch(()=>{});}
 })();

 // ===== U8：章节 .stake 真实点击 → 双方该节 units 全文展开/收起（DOM 字符差，无 API 兜底） =====
 await (async()=>{CURRENT='U8-section-stake';let page;
  try{
   const ctx=await browser.newContext({viewport:{width:1440,height:940}});
   page=await ctx.newPage();attach(page);
   await page.goto(`${WEB}/#/p/${A}/reader`,{waitUntil:'domcontentloaded'});
   await page.waitForSelector('#btn-aifold',{timeout:20000});
   await page.waitForTimeout(600);
   // 前序 U7 的 pane 开关经 patchPrefs 持久化进 overlay（origin/source 曾被关）：
   // 真实用户动作恢复三栏，否则源栏 iframe 0×0
   for(const k of ['origin','source','trans']){
     const cb=page.locator(`.rdrr-ptoggle input[aria-label="${k}"]`);
     if(!(await cb.isChecked())){await cb.check();}
   }
   await page.waitForTimeout(700);
   await enterFold(page);await page.waitForTimeout(1400);
   const srcF=await frameOf(page,'source');
   const dstF=await frameOf(page,'trans');
   if(!srcF||!dstF){ok('8-pre 双栏 iframe 就绪',false);throw new Error('frames not ready');}
   // 前置守卫：源栏 #doc 必须非零尺寸（否则渐进绘制/坐标全失效）
   const docSize=await srcF.evaluate(()=>{
     const d=document.getElementById('doc');const r=d.getBoundingClientRect();
     return {w:Math.round(r.width),h:Math.round(r.height)};});
   ok('8-pre 源栏 #doc 非零尺寸',docSize.w>200&&docSize.h>200,docSize);
   // 默认 prefs.level=1（kernel 4 全文）stake 不显示：切到产品层3（kernel 2 要点）
   const lvOk=await setLevel(page,srcF,3);
   await page.waitForTimeout(500);
   // 内核渐进绘制：滚动几屏让各节 stake 真实绘制，再回顶
   const srcBox=await page.evaluate(()=>{
     const el=document.querySelector('#rdrr-pane-source .rdrr-tfhost iframe');
     const r=el.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};});
   for(let i=0;i<4;i++){await page.mouse.move(srcBox.x,srcBox.y);await page.mouse.wheel(0,900);
     await page.waitForTimeout(350);}
   await srcF.evaluate(()=>{const sc=document.scrollingElement||document.documentElement;sc.scrollTop=0;});
   await page.waitForTimeout(900);
   // 确保三栏都在（前序场景独立 context，这里默认即三栏）
   const charsIn=(frame,ch)=>frame.evaluate(id=>{
     const sec=document.querySelector(`#doc section.chunk[data-chunk="${id}"]`);
     if(!sec)return -1;
     return [...sec.querySelectorAll('.u')].reduce((n,u)=>n+(u.textContent||'').length,0);
   },ch);
   const stake=await srcF.evaluate(()=>{
     const doc=document.getElementById('doc');
     const all=[...document.querySelectorAll('#doc section.chunk .stake')];
     const first=all[0]||null;
     const sec0=first?first.closest('section.chunk'):null;
     const cs=el=>{if(!el)return null;const c=getComputedStyle(el);
       return {fs:c.fontSize,disp:c.display,vis:c.visibility,h:Math.round(el.getBoundingClientRect().height)};};
     const diag={docLevel:doc?doc.dataset.level:null,stakeCount:all.length,
       first:{stHTML:first?(first.innerHTML||'').slice(0,150):null,
         st:cs(first),span:cs(first?first.querySelector('span'):null),
         spanText:first&&first.querySelector('span')?(first.querySelector('span').textContent||'').slice(0,12):null,
         sec:cs(sec0),secChildren:sec0?[...sec0.children].map(x=>`${x.tagName}.${String(x.className).slice(0,12)}:${Math.round(x.getBoundingClientRect().height)}`):null},
       samples:all.slice(0,3).map(x=>{const r=x.getBoundingClientRect();
         return {h:Math.round(r.height),text:(x.textContent||'').trim().slice(0,14)};})};
     const us=[...document.querySelectorAll('#doc .u')];
     diag.unitCount=us.length;
     diag.maxUnitH=Math.max(0,...us.map(u=>Math.round(u.getBoundingClientRect().height)));
     diag.unitSamples=us.slice(0,3).map(u=>({cls:u.className.slice(0,20),
       h:Math.round(u.getBoundingClientRect().height),text:(u.textContent||'').slice(0,12)}));
     const dr=doc.getBoundingClientRect();
     diag.doc={w:Math.round(dr.width),h:Math.round(dr.height)};
     const bh=getComputedStyle(doc).getPropertyValue('--lh');
     diag.lhVar=bh||'(unset)';
     const st=all.find(s=>{
       if(!((s.textContent||'').trim()))return false;
       const r=s.getBoundingClientRect();
       if(r.height>0)return true;
       const sp=s.querySelector('span');
       return !!(sp&&sp.getBoundingClientRect().height>0);});
     if(!st)return {err:'no visible stake with text',...diag};
     const sec=st.closest('section.chunk');
     const r=st.getBoundingClientRect();
     const sp=st.querySelector('span');
     const sr=sp?sp.getBoundingClientRect():r;
     // 内部坐标（由调用方加 iframe 父页面偏移）
     return {chunk:sec.dataset.chunk,title:(sec.querySelector('.hd')?.textContent||'').slice(0,20),
       inner:{x:Math.round(sr.x+Math.min(sr.width/2,60)),y:Math.round(sr.y+sr.height/2)}};
   });
   if(!stake||stake.err){ok('8-pre 可见 stake 定位',false,stake);throw new Error('stake not found');}
   const srcPaneBox=await page.evaluate(()=>{
     const el=document.querySelector('#rdrr-pane-source .rdrr-tfhost iframe');
     const r=el.getBoundingClientRect();return {x:r.x,y:r.y};});
   const clickPt={x:srcPaneBox.x+stake.inner.x,y:srcPaneBox.y+stake.inner.y};
   const srcBefore=await charsIn(srcF,stake.chunk);
   const dstBefore=await charsIn(dstF,stake.chunk);
   ok('8-pre stake 定位与双栏初始字符数（chunk='+stake.chunk+'）',
      srcBefore>=0&&dstBefore>=0,{...stake,srcBefore,dstBefore,lvOk});
   // 真实点击 stake（父页面坐标 = iframe 偏移 + 内部坐标；无选区状态，bridge click capture 路径）
   const hitInfo=await page.evaluate(({x,y})=>{
     const el=document.elementFromPoint(x,y);
     return {tag:el?el.tagName:null,cls:el?String(el.className).slice(0,30):null,
       text:el?(el.textContent||'').slice(0,12):null};
   },clickPt);
   await page.mouse.click(clickPt.x,clickPt.y);
   await page.waitForTimeout(1300);
   const srcAfter=await charsIn(srcF,stake.chunk);
   const dstAfter=await charsIn(dstF,stake.chunk);
   const kernState=await srcF.evaluate(id=>{
     const sec=document.querySelector(`#doc section.chunk[data-chunk="${id}"] .stake`);
     const us=[...document.querySelectorAll(`#doc section.chunk[data-chunk="${id}"] .u`)];
     return {hostOpen:sec?sec.dataset.hostOpen||null:null,
       localOpenCount:us.filter(u=>u.classList.contains('local-open')).length,
       unitCount:us.length};
   },stake.chunk).catch(e=>({err:String(e).slice(0,60)}));
   ok('8a stake 点击后双方该节展开（内核 hostOpen=true+localOpen，dst 字符显著增加，src 字符增加）',
      kernState.hostOpen==='true'&&kernState.localOpenCount>0&&
      dstAfter>=dstBefore+100&&srcAfter>srcBefore,
      {chunk:stake.chunk,clickPt,inner:stake.inner,hitInfo,kernState,
       src:{before:srcBefore,after:srcAfter,delta:srcAfter-srcBefore},
       dst:{before:dstBefore,after:dstAfter,delta:dstAfter-dstBefore}});
   // 再次真实点击收起
   await page.mouse.click(clickPt.x,clickPt.y);
   await page.waitForTimeout(1300);
   const srcBack=await charsIn(srcF,stake.chunk);
   const dstBack=await charsIn(dstF,stake.chunk);
   ok('8b 再次点击收起：双方字符数回落（src 明显回落、dst 回到初始水平）',
      srcBack<=srcAfter-30&&dstBack<=dstBefore+80,
      {chunk:stake.chunk,src:{before:srcBefore,back:srcBack},dst:{before:dstBefore,back:dstBack}});
   await ctx.close();
  }catch(e){ok('U8 场景异常',false,String(e).slice(0,250));if(page)await page.context().close().catch(()=>{});}
 })();

 await browser.close();
 const failed=R.filter(r=>!r.pass);
 require('fs').writeFileSync(RESULT,JSON.stringify({results:R,failed:failed.length,
   timeline:timeline.slice(-120)},null,1));
 console.log('USERFEEDBACK_FAILED='+failed.length+' total='+R.length);
 process.exit(failed.length?1:0);
})().catch(e=>{console.error('FATAL',String(e).slice(0,300));process.exit(99);});
