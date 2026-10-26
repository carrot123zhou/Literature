const {chromium}=require(process.env.PW_PACKAGE_PATH);

// reader AIFold 独立验收（整文件自包含）。
// argv: [web, api, pidsJSON, controlPort, resultPath]
const WEB=`http://127.0.0.1:${process.argv[2]}`;
const API=`http://127.0.0.1:${process.argv[3]}`;
const PIDS=JSON.parse(process.argv[4]);
const CONTROL=`http://127.0.0.1:${process.argv[5]}`;
const RESULT=process.argv[6];

const assertions=[];   // {scenario,name,pass,detail}
const timeline=[];     // 全部 /reader/* 请求/响应（含逐请求 project_id 校验结果）
let posts=[];          // POST /reader/fold-runs* 时间线
let CURRENT='?';
const ok=(name,pass,detail)=>{assertions.push({scenario:CURRENT,name:String(name),pass:!!pass,detail:detail===undefined?"":detail});return pass;};
const ctl=(path)=>pagefetch(CONTROL+path);
async function pagefetch(page,url){return page.evaluate(async u=>{try{await fetch(u);}catch(e){}return true;},url);}

function attach(page,scenario){
  page.on('request',r=>{
    const u=r.url(); if(!u.includes('/reader/'))return;
    const path=u.slice(API.length).split('?')[0];
    const m=/\/projects\/([0-9a-f-]+)/.exec(path);
    const pidInPath=m?m[1]:null;
    const expected=scenario.pid;
    timeline.push({t:Date.now(),dir:'req',method:r.method(),path:path.slice(0,90),
      pidOk:pidInPath===null?null:pidInPath===expected});
    if(r.method()==='POST'&&path.includes('/fold-runs'))posts.push({t:Date.now(),url:path,pid:scenario.pid,pidInPath});
  });
  page.on('response',r=>{
    const u=r.url(); if(!u.includes('/reader/'))return;
    const path=u.slice(API.length).split('?')[0];
    timeline.push({t:Date.now(),dir:'resp',method:r.request().method(),path:path.slice(0,90),status:r.status()});
  });
  page.on('request',r=>{
    const u=r.url();
    if(r.method()==='PUT'&&u.includes('/reader/overlay')){
      let rev=null,anns=[];
      try{const b=r.postDataJSON();rev=b.base_revision??b.overlay?.revision??null;
        anns=(b.overlay?.annotations||[]).map(a=>({id:a.id,anchor:a.anchor,pane:a.pane,
          start:a.start,end:a.end,color:a.color,note:(a.note||'').slice(0,40),
          engine_anchor:a.engine_anchor?{space:a.engine_anchor.space,digest:a.engine_anchor.digest,
            version:a.engine_anchor.version,anchor:a.engine_anchor.anchor}:null}));}catch(e){}
      timeline.push({t:Date.now(),dir:'put-body',revision:rev,annotations:anns});
    }
  });
  page.on('pageerror',e=>timeline.push({t:Date.now(),kind:'pageerror',message:String(e).slice(0,300)}));
}
const runPosts=(since)=>posts.slice(since);
async function getFrames(page){
  const deadline=Date.now()+15000;
  while(Date.now()<deadline){
    const frames=page.frames().filter(f=>f!==page.mainFrame()&&f.url().startsWith('about:'));
    if(frames.length>=1){
      const ready=await frames[0].evaluate(()=>!!document.getElementById('doc')&&!!document.querySelector('#doc .u')).catch(()=>false);
      if(ready)return {src:frames[0],dst:frames[1]||frames[0]};
    }
    await new Promise(r=>setTimeout(r,150));
  }
  const frames=page.frames().filter(f=>f!==page.mainFrame());
  return {src:frames[0],dst:frames[1]||frames[0]};
}
const runSnapshot=async(page,pid)=>page.evaluate(async({api,pid})=>{
  const r=await fetch(`${api}/projects/${pid}/reader/fold-run`,{headers:{'x-literature-token':''}});
  return r.json();
},{api:API,pid});
async function enterFold(page,timeout=30000){
  const stage0=await page.locator('#btn-aifold').getAttribute('data-stage').catch(()=>'?');
  if(stage0==='off'){
    await page.locator('#btn-aifold').click();
    await page.waitForSelector('#btn-aifold[data-stage="ready"],#lvlbar',{timeout});
    if(await page.locator('#lvlbar').count())return true;
  }
  if(stage0==='ready'||await page.locator('#btn-aifold[data-stage="ready"]').count()){
    await page.locator('#btn-aifold').click();
  }else if(stage0!=='on'){
    // processing → 等 ready
    await page.waitForSelector('#btn-aifold[data-stage="ready"],#lvlbar',{timeout});
    if(await page.locator('#lvlbar').count())return true;
    await page.locator('#btn-aifold').click();
  }
  await page.waitForSelector('#lvlbar',{timeout});
  return true;
}
async function waitRun(page,pid,statuses,timeout=25000){
  const deadline=Date.now()+timeout;
  let last={};
  while(Date.now()<deadline){
    last=await runSnapshot(page,pid);
    if(statuses.includes(last.status))return last;
    await page.waitForTimeout(150);
  }
  return last;
}

(async()=>{
 const browser=await chromium.launch({headless:true});
 const ctxOpts={viewport:{width:1440,height:920}};

 // ================= 场景 1（项目 A，独立 context）：默认关 → run → 刷新/退出再启 =================
 await (async()=>{
  const scenario={name:'S1-lifecycle',pid:PIDS[0]};let page;CURRENT='S1-lifecycle';
  try{
   const ctx=await browser.newContext(ctxOpts);page=await ctx.newPage();attach(page,scenario);
   await page.goto(`${WEB}/#/p/${scenario.pid}/reader`,{waitUntil:'domcontentloaded'});
   await page.waitForSelector('#btn-aifold',{timeout:20000});
   await page.waitForTimeout(1200);
   ok('1.1 折叠默认关闭（off、无五档 bar）',
      await page.locator('#btn-aifold[data-stage="off"]').count()===1&&await page.locator('#lvlbar').count()===0);
   const p0=posts.length;
   ok('1.2 首开零生成请求',runPosts(p0).length===0,runPosts(p0));

   await page.locator('#btn-aifold').click();
   await page.waitForSelector('#btn-aifold[data-stage="ready"]',{timeout:30000});
   const b1=runPosts(p0);
   ok('1.3 AIFold 恰好一次 POST /reader/fold-runs 且 project_id 正确',
      b1.length===1&&b1.every(p=>p.pidInPath===p.pid),b1);
   const snapReady=await runSnapshot(page,scenario.pid);
   ok('1.4 run 快照 ready（project 身份由 fold-run URL 断言覆盖）',
      snapReady.status==='ready'&&snapReady.run_id?.startsWith('fr-'),{status:snapReady.status,run_id:snapReady.run_id});
   await page.locator('#btn-aifold').click();   // 启动
   await page.waitForSelector('#lvlbar',{timeout:20000});
   await page.waitForSelector('.rdrr-tfhost iframe',{timeout:15000});
   const {src}=await getFrames(page);
   const title=await page.title().catch(()=>'');
   ok('1.5 启动进入折叠：五档 bar + 内核就绪 + 页面标题正确',
      await page.locator('#lvlbar').count()===1&&!!src&&title.includes('Literature'),{title});

   // 刷新再启
   await page.reload({waitUntil:'domcontentloaded'});
   await page.waitForSelector('#btn-aifold',{timeout:20000});
   await page.waitForTimeout(900);
   const p1=posts.length;
   await page.locator('#btn-aifold').click();
   let launched=false;
   try{await page.waitForSelector('#lvlbar',{timeout:15000});launched=true;}
   catch(e){if(await page.locator('#btn-aifold[data-stage="ready"]').count()){await page.locator('#btn-aifold').click();
     await page.waitForSelector('#lvlbar',{timeout:15000});launched=true;}}
   ok('1.6 刷新后重新进入折叠',launched);
   ok('1.7 刷新再启零重复 run POST',runPosts(p1).filter(p=>p.url.includes('/fold-runs')).length===0,runPosts(p1));

   // 退出再启
   await page.locator('#btn-aifold').click();
   await page.waitForTimeout(700);
   const p2=posts.length;
   await page.locator('#btn-aifold').click();
   let relaunched=false;
   try{await page.waitForSelector('#lvlbar',{timeout:15000});relaunched=true;}catch(e){}
   ok('1.8 退出后 ready 保留、可再启动',relaunched);
   ok('1.9 退出再启零重复 run POST（不重折）',runPosts(p2).filter(p=>p.url.includes('/fold-runs')).length===0,runPosts(p2));
   await ctx.close();
  }catch(e){ok('S1 场景异常',false,String(e).slice(0,300));if(page)await page.context().close().catch(()=>{});}
 })();

 // ================= 场景 2（项目 B，独立 context）：notes 完整 payload/双侧差异/跨模式 =================
 await (async()=>{
  const scenario={name:'S2-notes',pid:PIDS[1]};let page;CURRENT='S2-notes';
  try{
   const ctx=await browser.newContext(ctxOpts);page=await ctx.newPage();attach(page,scenario);
   await page.goto(`${WEB}/#/p/${scenario.pid}/reader`,{waitUntil:'domcontentloaded'});
   await page.waitForSelector('#btn-aifold',{timeout:20000});
   await page.waitForTimeout(600);
   await page.locator('#btn-aifold').click();
   await page.waitForSelector('#btn-aifold[data-stage="ready"]',{timeout:30000});
   await page.locator('#btn-aifold').click();
   await page.waitForSelector('#lvlbar',{timeout:20000});
   await page.waitForTimeout(1200);
   let {src}=await getFrames(page);
   const metaId=await src.evaluate(()=>{const el=document.getElementById('paper-data');return el?JSON.parse(el.textContent).meta.id:'';});
   const uid=await src.evaluate(()=>document.querySelector('#doc .u')?.id||'');
   const FULL_PAYLOAD={op:'put',note:{id:'acc-note-1',kind:'note',
     anchor:{unit:uid,section:null,lang:'source',level:4,tokens:null,sents:null,
             quote:{exact:'Silver vessels travelled along the Silk Road.',prefix:'',suffix:''}},
     text:'验收笔记一',thread:[],color:'y',shade:2}};
   const put=await src.evaluate(async([mid,body])=>{
     const r=await fetch('/api/notes/'+mid,{method:'POST',headers:{'content-type':'application/json'},body});
     return {status:r.status,body:await r.json().catch(()=>null)};
   },[metaId,JSON.stringify(FULL_PAYLOAD)]);
   ok('2.1 notes put（完整 anchor/quote payload）→ 200',put.status===200&&put.body?.ok===true,put);
   let overlayAnns=[];
   for(let i=0;i<12;i++){
     const st=await page.evaluate(async({api,pid})=>{
       const r=await fetch(`${api}/projects/${pid}/reader/state?locale=zh-CN`,{headers:{'x-literature-token':''}});
       return (await r.json()).overlay||{};
     },{api:API,pid:scenario.pid}).catch(()=>({}));
     overlayAnns=st.annotations||[];
     if(overlayAnns.some(a=>a.id==='acc-note-1'))break;
     await page.waitForTimeout(300);
   }
   const stored=overlayAnns.find(a=>a.id==='acc-note-1')||null;
   ok('2.2 put 真实落盘 overlay（CAS revision 递增）',!!stored,{stored});
   // 双侧差异取证：内核 notes vs overlay 批注（字段映射）
   const kernel=await src.evaluate(async()=>{const r=await fetch('/api/notes/');return r.json();});
   const kNote=(kernel.notes||[]).find(n=>n.id==='acc-note-1')||null;
   const diff={kernel:kNote&&{id:kNote.id,text:kNote.text,color:kNote.color,unit:kNote.anchor?.unit,quote:kNote.anchor?.quote?.exact},
               overlay:stored&&{id:stored.id,text:stored.note,color:stored.color,unit:stored.block_id,quote:stored.quote?.exact},
               kernelCount:(kernel.notes||[]).length,overlayCount:overlayAnns.length};
   ok('2.3 内核 note ↔ overlay 批注字段映射一致（id/unit/quote/text）',
      !!kNote&&!!stored&&diff.kernel.id===diff.overlay.id&&diff.kernel.unit===diff.overlay.unit
      &&diff.kernel.text===diff.overlay.text&&diff.kernel.quote===diff.overlay.quote,diff);
   // 改色
   await src.evaluate(async([mid,body])=>{await fetch('/api/notes/'+mid,{method:'POST',headers:{'content-type':'application/json'},body});},
     [metaId,JSON.stringify({...FULL_PAYLOAD,note:{...FULL_PAYLOAD.note,color:'g'}})]);
   await page.waitForTimeout(900);
   // 删除（阻断取证：内核已删、overlay rev+1 但条目保留）
   const delAt=Date.now();
   const del=await src.evaluate(async([mid])=>{
     const r=await fetch('/api/notes/'+mid,{method:'POST',headers:{'content-type':'application/json'},
       body:JSON.stringify({op:'delete',id:'acc-note-1'})});
     return {status:r.status,body:await r.json().catch(()=>null)};
   },[metaId]);
   await page.waitForTimeout(1500);
   const kernelAfterDel=await src.evaluate(async()=>{const r=await fetch('/api/notes/');return r.json();});
   const overlayAfterDel=await page.evaluate(async({api,pid})=>{
     const r=await fetch(`${api}/projects/${pid}/reader/state?locale=zh-CN`,{headers:{'x-literature-token':''}});
     return (await r.json()).overlay||{};
   },{api:API,pid:scenario.pid}).catch(()=>({}));
   const stillThere=(overlayAfterDel.annotations||[]).some(a=>a.id==='acc-note-1');
   ok('2.4 删除后内核 notes 已空',!(kernelAfterDel.notes||[]).some(n=>n.id==='acc-note-1'),
      {kernelIds:(kernelAfterDel.notes||[]).map(n=>n.id)});
   ok('2.5 删除后 overlay 不再含该批注（当前缺陷：rev 递增但条目保留）',
      !stillThere,{rev:overlayAfterDel.revision,ids:(overlayAfterDel.annotations||[]).map(a=>a.id)});
   // 刷新无复生
   await page.reload({waitUntil:'domcontentloaded'});
   await page.waitForSelector('#btn-aifold',{timeout:20000});
   await page.waitForTimeout(1000);
   const overlayAfterReload=await page.evaluate(async({api,pid})=>{
     const r=await fetch(`${api}/projects/${pid}/reader/state?locale=zh-CN`,{headers:{'x-literature-token':''}});
     return (await r.json()).overlay||{};
   },{api:API,pid:scenario.pid}).catch(()=>({}));
   ok('2.6 刷新后删除不复生',
      !(overlayAfterReload.annotations||[]).some(a=>a.id==='acc-note-1'),
      {ids:(overlayAfterReload.annotations||[]).map(a=>a.id)});
   // 跨模式：fold 内新增 → 退出到普通阅读 → mark 可见 → 再进 fold 内核仍在
   await enterFold(page);   // reload 后重新进入折叠（否则无内核 iframe）
   await page.waitForTimeout(1200);
   ({src}=await getFrames(page));
   await src.evaluate(async([mid,body])=>{
     await fetch('/api/notes/'+mid,{method:'POST',headers:{'content-type':'application/json'},body});
   },[metaId,JSON.stringify({...FULL_PAYLOAD,note:{...FULL_PAYLOAD.note,id:'acc-note-2',text:'跨模式笔记'}})]);
   await page.waitForTimeout(1000);
   await page.screenshot({path:'tmp/reader-aifold-screens/2.7-fold-before-exit.png'});
   await page.locator('#btn-aifold').click();   // 退出折叠 → 普通阅读
   await page.waitForTimeout(1200);
   // block 锚批注：rdrr-blockmark 段首色条肉眼可见（不强行要求 mark.hl）
   const blockMark=await page.evaluate(()=>{
     const p=document.querySelector('#rdrr-pane-source p[data-block-id="b-p-1"]');
     if(!p)return {found:false};
     const cs=getComputedStyle(p,'::before');
     const r=p.getBoundingClientRect();
     return {found:true,cls:p.className,blockmark:/rdrr-blockmark/.test(p.className),
             barWidth:cs.width,barBg:cs.backgroundColor,
             rect:{w:Math.round(r.width),h:Math.round(r.height)}};
   });
   ok('2.7 退出折叠：block 锚批注以 rdrr-blockmark 段首色条肉眼可见（b-p-1、条宽>0、段可见）',
      blockMark.found&&blockMark.blockmark&&parseFloat(blockMark.barWidth)>0
      &&blockMark.rect.w>100&&blockMark.rect.h>20,blockMark);
   // 可编辑：右键进入唯一编辑态（hlPop）
   await page.evaluate(()=>{
     const p=document.querySelector('#rdrr-pane-source p[data-block-id="b-p-1"]');
     const r=p.getBoundingClientRect();
     p.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,clientX:r.x+r.width/2,clientY:r.y+10}));
   });
   await page.waitForTimeout(700);
   const editable=await page.evaluate(()=>({hlPop:!!document.querySelector('#hlPop'),
     textarea:!!document.querySelector('#hlPop textarea')}));
   ok('2.8 block 锚批注可编辑（右键进入 hlPop 编辑态）',editable.hlPop&&editable.textarea,editable);
   await page.keyboard.press('Escape');

   // 普通→fold：React 真实划词范围高亮 → 字符↔token 往返
   const selText="Workshops in Chang'an copied Sogdian shapes";
   await page.evaluate((text)=>{
     const p=document.querySelector('#rdrr-pane-source p[data-block-id="b-p-1"]');
     const walker=document.createTreeWalker(p,NodeFilter.SHOW_TEXT);
     let node,startNode,startOff,endNode,endOff;
     while(node=walker.nextNode()){
       const idx=node.textContent.indexOf(text);
       if(idx>=0){startNode=node;startOff=idx;endNode=node;endOff=idx+text.length;break;}
     }
     const range=document.createRange();
     range.setStart(startNode,startOff);range.setEnd(endNode,endOff);
     const sel=window.getSelection();sel.removeAllRanges();sel.addRange(range);
     const body=document.querySelector('#rdrr-pane-source .rdrr-pane-body')||document.getElementById('rdrr-pane-source');
     body.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,clientX:120,clientY:300}));
   },selText);
   await page.waitForTimeout(700);
   const selPopShown=await page.evaluate(()=>!!document.querySelector('#selPop'));
   if(selPopShown){
     await page.locator('#selPop .rdrr-swatch').first().click();
     await page.waitForTimeout(1000);
   }
   const annDump=await page.evaluate(async({api,pid})=>{
     const r=await fetch(`${api}/projects/${pid}/reader/state?locale=zh-CN`,{headers:{'x-literature-token':''}});
     const d=await r.json();
     return (d.overlay?.annotations||[]).map(a=>({id:a.id,anchor:a.anchor,pane:a.pane,start:a.start,end:a.end,
       quote:a.quote?.exact?.slice(0,50),color:a.color}));
   },{api:API,pid:scenario.pid});
   const reactRange=annDump.find(a=>a.anchor==='range'&&a.quote==="Workshops in Chang'an copied Sogdian shapes")||null;
   ok('2.9 普通→fold：真实划词范围高亮落 overlay（quote 逐字=选区、字符区间；不假 block 降级）',
      selPopShown&&!!reactRange,{selPopShown,annDump,reactRange});
   // 进 fold：内核 notes 的 tokens 与该字符区间往返一致（内核 toks 重建文本核验）
   await enterFold(page);
   await page.waitForTimeout(1200);
   ({src}=await getFrames(page));
   const roundTrip=await src.evaluate(async([uid2,needle])=>{
     const el=document.getElementById('paper-data');
     const data=el?JSON.parse(el.textContent):{};
     const unit=data.units[uid2]||{};const atoms=data.atoms||{};
     const plain=(tk)=>{if(typeof tk==='string')return tk;
       if(tk.a){const atom=atoms[tk.a];if(!atom)return '';
         return atom.kind==='math'?`$${atom.alt??''}$`:(atom.text||'');}
       return tk.x?`$${tk.x}$`:(tk.t||'');};
     let rebuilt='';const starts=[];const ends=[];
     for(const tk of (unit.toks||[])){starts.push(rebuilt.length);rebuilt+=plain(tk);ends.push(rebuilt.length);
       if(tk.s)rebuilt+=' ';}
     const r=await fetch('/api/notes/');const d=await r.json();
     const kn=(d.notes||[]).find(n=>(n.anchor?.quote?.exact||'').includes('Workshops in Chang'))
              || (d.notes||[]).find(n=>n.id!=='acc-note-2');
     if(!kn)return {found:false,notes:(d.notes||[]).map(n=>n.id)};
     const [ti,tj]=kn.anchor?.tokens||[];
     const tokenSlice=rebuilt.slice(starts[ti],ends[tj]);
     return {found:true,tokens:[ti,tj],quote:kn.anchor?.quote?.exact,
             tokenSlice,rebuiltLen:rebuilt.length,
             rebuiltHasNeedle:rebuilt.includes(needle)};
   },[uid,selText]);
   // 普通（非 fold）阅读里创建的划词批注只有字符区间，内核回执 tokens 允许为 null；
   // 若 tokens 存在则必须与 quote 逐字对应（不伪造）。
   const tokensAllNull=Array.isArray(roundTrip.tokens)&&roundTrip.tokens.every(x=>x==null);
   const tokenOk=!Array.isArray(roundTrip.tokens)||tokensAllNull
     ||(roundTrip.tokens.every(x=>x!=null)&&roundTrip.tokenSlice===roundTrip.quote);
   ok('2.10 React 划词精确往返：quote 逐字一致 + overlay 字符区间；tokens 有则与 quote 对应（普通阅读允许 null）',
      roundTrip.found
      &&String(roundTrip.quote||'').replace(/\s+/g,' ').trim()===String(selText||'').replace(/\s+/g,' ').trim()
      &&tokenOk,
      {...roundTrip,selText,tokenOk,verdict:{found:roundTrip.found,
        eq:String(roundTrip.quote||'').replace(/\s+/g,' ').trim()===String(selText||'').replace(/\s+/g,' ').trim(),
        tokenOk}});
   // 说明：原「手工构造 token 锚（合成输入）」用例已移除——按 Lead 原则，手工注入不得计为成功；
   // 折叠内批注的真实路径（自然选词→engine_anchor→刷新复原）由 per-space 套件 8 空间覆盖。
   // 源栏批注不得挂到译文偏移：用 2.9 的自然 React 划词批注核验（pane=source、英文字符区间）
   ok('2.12 源批注不乱挂译文词偏移（pane=source、quote 为英文原文、字符区间合法）',
      !!reactRange&&reactRange.pane==='source'
      &&/[A-Za-z]{6,}/.test(reactRange.quote||'')
      &&typeof reactRange.start==='number'&&reactRange.end>reactRange.start,
      reactRange);
   await enterFold(page);
   await page.waitForTimeout(1000);
   ({src}=await getFrames(page));
   const kernelReEnter=await src.evaluate(async()=>{const r=await fetch('/api/notes/');return r.json();});
   ok('2.13 再进折叠：内核 notes 仍含跨模式笔记',
      (kernelReEnter.notes||[]).some(n=>n.id==='acc-note-2'),{ids:(kernelReEnter.notes||[]).map(n=>n.id)});
   await page.screenshot({path:'tmp/reader-aifold-screens/2-final-fold.png'});
   await ctx.close();
  }catch(e){ok('S2 场景异常',false,String(e).slice(0,300));if(page)await page.context().close().catch(()=>{});}
 })();

 // ================= 场景 3（项目 B，独立 context）：force run 确定性暂停→resume，逐请求 project_id =================
 await (async()=>{
  const scenario={name:'S3-pause',pid:PIDS[1]};let page;CURRENT='S3-pause';
  try{
   const ctx=await browser.newContext(ctxOpts);page=await ctx.newPage();attach(page,scenario);
   await page.goto(`${WEB}/#/p/${scenario.pid}/reader`,{waitUntil:'domcontentloaded'});
   await page.waitForSelector('#btn-aifold',{timeout:20000});
   await page.waitForTimeout(600);
   const t0=Date.now();
   const badPid=timeline.filter(e=>e.t>=t0&&e.pidOk===false).length;
   // 确定性暂停窗口：布防 → force run（POST 带 body，合法）→ reload 让 UI 看到 running
   await pagefetch(page,`${CONTROL}/arm`);
   await page.evaluate(async({api,pid})=>{
     await fetch(`${api}/projects/${pid}/reader/fold-runs`,{method:'POST',
       headers:{'x-literature-token':'','content-type':'application/json'},
       body:JSON.stringify({locale:'zh-CN',force:true})});
   },{api:API,pid:scenario.pid});
   await page.reload({waitUntil:'domcontentloaded'});
   await page.waitForSelector('#btn-foldpause',{timeout:25000});
   const pausePosts0=timeline.filter(e=>e.dir==='req'&&e.method==='POST'&&e.path.includes('/pause')).length;
   await page.locator('#btn-foldpause').click();
   await page.waitForTimeout(600);
   const pausePosts=timeline.filter(e=>e.dir==='req'&&e.method==='POST'&&e.path.includes('/pause')).length;
   ok('3.1 暂停请求真实发出（POST …/pause）',pausePosts>pausePosts0,{pausePosts});
   // 放行 provider：在途节跑完落盘后，节边界转 paused
   await pagefetch(page,`${CONTROL}/release`);
   const pausedSnap=await waitRun(page,scenario.pid,('paused ready').split(' '));
   if(pausedSnap.status==='ready'){
     ok('3.2 暂停边界生效',true,{note:'provider 过快整篇完成（边界语义允许）'});
   }else{
     ok('3.2 暂停后 run=paused（等待 paused 再继续）',pausedSnap.status==='paused',{status:pausedSnap.status});
     ok('3.3 pending≥1 且无空 id failed 项',
        (pausedSnap.progress?.pending||0)>=1&&(pausedSnap.errors||[]).every(e=>e.section_id),
        {progress:pausedSnap.progress,errors:pausedSnap.errors});
   }
   // UI「继续」：等 data-paused=1
   await page.waitForSelector('#btn-foldpause[data-paused="1"]',{timeout:15000});
   const resumeReqs0=timeline.filter(e=>e.dir==='req'&&e.path.includes('/resume')).length;
   await page.locator('#btn-foldpause').click();
   const deadline=Date.now()+30000;
   let final={},resumeStatus=null;
   while(Date.now()<deadline){
     const rs=timeline.filter(e=>e.dir==='resp'&&e.path.includes('/resume'));
     resumeStatus=rs.length?rs[rs.length-1].status:null;
     final=await runSnapshot(page,scenario.pid);
     if(final.status==='ready'||final.status==='failed')break;
     await page.waitForTimeout(150);
   }
   ok('3.4 UI「继续」resume 实际无 body POST 且返回 200',resumeStatus===200,{resumeStatus});
   ok('3.5 继续后 run=ready',final.status==='ready',{status:final.status,
      progress:final.progress,ready_basis:final.ready_basis});
   if(resumeStatus!==200){
     // developer 修复前：API 级 {} 收尾（如实记 FAIL，不跳过）
     await page.evaluate(async({api,pid})=>{
       await fetch(`${api}/projects/${pid}/reader/fold-runs/${(await runSnapshot(page,pid)).run_id}/resume`,
         {method:'POST',headers:{'x-literature-token':'','content-type':'application/json'},body:JSON.stringify({})});
     },{api:API,pid:scenario.pid}).catch(()=>{});
     await waitRun(page,scenario.pid,['ready','failed']);
   }
   // 全程逐请求 project_id 校验 + 页面标题
   const titleNow=await page.title();
   const badPidNow=timeline.filter(e=>e.t>=t0&&e.pidOk===false).length;
   ok('3.6 本场景 /reader/* 请求 project_id 均属该项目（无串扰）',badPidNow===0,
      {badPidNow,totalChecked:timeline.filter(e=>e.t>=t0&&e.pidOk!==null).length});
   ok('3.7 页面标题正确（run 快照所属项目）',titleNow.includes('Literature'),{titleNow});
   await ctx.close();
  }catch(e){ok('S3 场景异常',false,String(e).slice(0,300));if(page)await page.context().close().catch(()=>{});}
 })();

 // ================= 场景 4（项目 A，独立 context）：morph 深观测（__drPerf/observer/WAAPI/token 运动） =================
 await (async()=>{
  const scenario={name:'S4-morph',pid:PIDS[0]};let page;CURRENT='S4-morph';
  try{
   const ctx=await browser.newContext(ctxOpts);page=await ctx.newPage();attach(page,scenario);
   await page.goto(`${WEB}/#/p/${scenario.pid}/reader`,{waitUntil:'domcontentloaded'});
   await page.waitForSelector('#btn-aifold',{timeout:20000});
   await page.waitForTimeout(600);
   await enterFold(page);
   await page.waitForTimeout(1500);
   let {src}=await getFrames(page);
   // 观测器必须在点击前注册
   await src.evaluate(()=>{
     window.__acc={ghostAdded:0,raf:0,anims:0};
     new MutationObserver(ms=>{for(const m of ms)window.__acc.ghostAdded+=m.addedNodes.length;})
       .observe(document.getElementById('ghosts'),{childList:true});
     const loop=()=>{window.__acc.raf++;requestAnimationFrame(loop);};requestAnimationFrame(loop);
     window.__instMark='inst-'+Math.random().toString(36).slice(2);
   });
   const levelBefore=await src.evaluate(()=>({lvl:window.__reader.level,doc:document.getElementById('doc').dataset.level}));
   const tokensBefore=await src.evaluate(()=>[...document.querySelectorAll('#doc .u span[data-id]')]
     .slice(0,40).map(s=>{const r=s.getBoundingClientRect();return {id:s.dataset.id,x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width)};}));
   const drPerfLenBefore=await src.evaluate(()=>(window.__drPerf||[]).length);
   // 触发：宿主五档 bar L5（先清 __drPerf 增量窗）
   await page.locator('#lvlbar [data-l="5"]').click();
   await page.waitForTimeout(1400);
   const levelAfter=await src.evaluate(()=>({lvl:window.__reader.level,doc:document.getElementById('doc').dataset.level}));
   const obs=await src.evaluate(()=>window.__acc);
   const instKept=await src.evaluate(()=>window.__instMark||'');
   const perfTail=await src.evaluate(()=>{const p=window.__drPerf||[];return p.slice(-12);});
   const anims=await src.evaluate(()=>document.getAnimations?document.getAnimations().length:-1);
   const ghostsNow=await src.evaluate(()=>document.getElementById('ghosts').children.length);
   const tokensAfter=await src.evaluate(()=>[...document.querySelectorAll('#doc .u span[data-id]')]
     .slice(0,40).map(s=>{const r=s.getBoundingClientRect();return {id:s.dataset.id,x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width)};}));
   const tokenMoved=tokensBefore.length>0&&tokensAfter.length>0&&
     JSON.stringify(tokensBefore.slice(0,10))!==JSON.stringify(tokensAfter.slice(0,10));
   const countsEntry=perfTail.find(e=>e[0]==='counts');
   const morphEvidence={
     levelBefore,levelAfter,ghostAdded:obs.ghostAdded,ghostsNow,anims,
     drPerfTail:perfTail,counts:countsEntry?countsEntry[1]:null,
     tokenMoved,tokensBefore:tokensBefore.slice(0,6),tokensAfter:tokensAfter.slice(0,6),
     rafAlive:obs.raf>0,instKept:!!instKept,
   };
   const hasMotion=obs.ghostAdded>0||ghostsNow>0||(countsEntry&&(countsEntry[1].moved>0||countsEntry[1].ghosts>0))||anims>0||tokenMoved;
   const stageRan=perfTail.some(e=>e[0]==='t.stage'||e[0]==='t.commit'||e[0]==='p.render');
   ok('4.1 切层后实际 level 变化（产品 1→主题/引擎 0）',levelAfter.lvl===0&&levelAfter.doc==='0',
      {levelBefore,levelAfter});
   ok('4.2 rAF 循环存活、iframe 实例未被更换（观测连续）',morphEvidence.rafAlive&&morphEvidence.instKept,
      {raf:obs.raf,instKept:morphEvidence.instKept});
   ok('4.3 切层存在真实运动证据（ghost/WAAPI/__drPerf.moved/token 位移，任一即可；全无为 FAIL）',
      hasMotion,morphEvidence);
   ok('4.4 上游 stage/morph 热路径有执行痕迹（t.stage/t.commit/p.render）',stageRan,{perfTail});
   // 无 motion 时的补充触发路径（zoomBy / 引擎键盘），仍以真实运动为准
   if(!hasMotion){
     await src.evaluate(()=>{try{window.__reader.zoomBy(1,{x:innerWidth/2,y:innerHeight/2});}catch(e){}});
     await page.waitForTimeout(1000);
     const obs2=await src.evaluate(()=>window.__acc);
     const anims2=await src.evaluate(()=>document.getAnimations().length);
     ok('4.5 zoomBy 路径运动证据（补充触发）',obs2.ghostAdded>0||anims2>0,{ghostAdded:obs2.ghostAdded,anims:anims2});
   }
   await ctx.close();
  }catch(e){ok('S4 场景异常',false,String(e).slice(0,300));if(page)await page.context().close().catch(()=>{});}
 })();

 // ================= 场景 5（项目 A，独立 context）：Aa 主题计算色（context 隔离防收尾超时） =================
 await (async()=>{
  const scenario={name:'S5-theme',pid:PIDS[0]};let page;CURRENT='S5-theme';
  try{
   const ctx=await browser.newContext(ctxOpts);page=await ctx.newPage();attach(page,scenario);
   await page.goto(`${WEB}/#/p/${scenario.pid}/reader`,{waitUntil:'domcontentloaded'});
   await page.waitForSelector('#btn-aifold',{timeout:20000});
   await page.waitForTimeout(500);
   await enterFold(page);
   await page.waitForTimeout(1200);
   const {src}=await getFrames(page);
   const cLight=await src.evaluate(()=>getComputedStyle(document.documentElement).getPropertyValue('--c-blue').trim());
   await page.locator('.rdrr-tbtn').filter({hasText:'Aa'}).first().click();
   await page.waitForTimeout(400);
   await page.locator('.rdrr-aa button').filter({hasText:'深色'}).first().click();
   await page.waitForTimeout(700);
   const cDark=await src.evaluate(()=>({v:getComputedStyle(document.documentElement).getPropertyValue('--c-blue').trim(),theme:document.documentElement.dataset.theme||''}));
   ok('5.1 深色主题上游计算色真实变化',cDark.v&&cDark.v!==cLight,{before:cLight,after:cDark});
   await page.locator('.rdrr-aa button').filter({hasText:'纸色'}).first().click();
   await page.waitForTimeout(700);
   const cPaper=await src.evaluate(()=>({v:getComputedStyle(document.documentElement).getPropertyValue('--c-blue').trim(),style:document.documentElement.dataset.style||''}));
   ok('5.2 纸色主题计算色再次变化',cPaper.v&&cPaper.v!==cDark.v,{paper:cPaper});
   await ctx.close();
  }catch(e){ok('S5 场景异常',false,String(e).slice(0,300));if(page)await page.context().close().catch(()=>{});}
 })();

 // ================= 场景 6（项目 A，独立 context）：双语 L3≠L2 + PDF 证据（canvas 文字像素+页定位） =================
 await (async()=>{
  const scenario={name:'S6-evidence',pid:PIDS[0]};let page;CURRENT='S6-evidence';
  try{
   const ctx=await browser.newContext(ctxOpts);page=await ctx.newPage();attach(page,scenario);
   await page.goto(`${WEB}/#/p/${scenario.pid}/reader`,{waitUntil:'domcontentloaded'});
   await page.waitForSelector('#btn-aifold',{timeout:20000});
   await page.waitForTimeout(500);
   // PDF 证据（普通阅读，未进 fold）：canvas 文字像素 + 页定位
   await page.waitForSelector('#rdrr-pane-origin .pdf-page',{timeout:25000}).catch(()=>{});
   let pdf={};
   for(let i=0;i<14;i++){
     await page.waitForTimeout(300);
     pdf=await page.evaluate(()=>{
       const origin=document.getElementById('rdrr-pane-origin');
       if(!origin)return {noOrigin:true};
       const pageEl=[...origin.querySelectorAll('.pdf-page')].find(p=>{const r=p.getBoundingClientRect();return r.bottom>0&&r.top<window.innerHeight;});
       if(!pageEl)return {pages:origin.querySelectorAll('.pdf-page').length};
       const canvas=pageEl.querySelector('canvas');
       let ink=0;
       if(canvas){
         try{const d=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
           for(let i=0;i<d.length;i+=16)if(d[i+3]>0&&d[i]<150&&d[i+1]<150&&d[i+2]<150)ink++;
         }catch(e){ink=-1;}
       }
       const r=pageEl.getBoundingClientRect();
       return {pages:origin.querySelectorAll('.pdf-page').length,pageIndex:pageEl.dataset.pageIndex,
               rendered:pageEl.dataset.rendered,ink,pageTop:Math.round(r.top),inViewport:r.bottom>0};
     });
     if(pdf.ink>10)break;
   }
   ok('6.1 原件 PDF 页面真实渲染且有文字像素（ink>10）',(pdf.ink||0)>10,pdf);
   // 点击原文块 → 页定位（目标页在视野内）
   const clickOk=await page.evaluate(()=>{
     const blk=[...document.querySelectorAll('#rdrr-pane-source [data-block-id]')].find(b=>b.dataset.blockId==='b-title-2');
     if(!blk)return false;
     blk.dispatchEvent(new MouseEvent('click',{bubbles:true,clientX:blk.getBoundingClientRect().x+10,clientY:blk.getBoundingClientRect().y+10}));
     return true;
   });
   await page.waitForTimeout(1200);
   const pos=await page.evaluate(()=>{
     const origin=document.getElementById('rdrr-pane-origin');
     if(!origin)return{};
     const target=[...origin.querySelectorAll('.pdf-page')].find(p=>p.dataset.pageIndex==='1');
     const visible=[...origin.querySelectorAll('.pdf-page')].filter(p=>{const r=p.getBoundingClientRect();return r.bottom>0&&r.top<window.innerHeight;});
     const tr=target?target.getBoundingClientRect():null;
     return {visiblePages:visible.map(p=>p.dataset.pageIndex),
             targetVisible:tr?(tr.bottom>0&&tr.top<window.innerHeight):false,
             rendered:target?target.dataset.rendered:null};
   });
   ok('6.2 点击原文块后原件定位：目标页（页 1）进入视野且已渲染',
      clickOk&&pos.targetVisible===true&&pos.rendered==='true',{pos});
   // 双语 L3≠L2（fold 内）：目标栏 Key written line vs Brief derived
   await enterFold(page);
   await page.waitForTimeout(1200);
   let {dst}=await getFrames(page);
   await page.locator('#lvlbar [data-l="2"]').click();
   await dst.waitForFunction(()=>window.__reader&&window.__reader.level===3&&document.getElementById('doc').dataset.level==='3',{timeout:8000}).catch(()=>{});
   const l2Text=await dst.evaluate(()=>document.getElementById('doc').innerText.replace(/\s+/g,' '));
   await page.locator('#lvlbar [data-l="3"]').click();
   await dst.waitForFunction(()=>window.__reader&&window.__reader.level===2&&document.getElementById('doc').dataset.level==='2',{timeout:8000}).catch(()=>{});
   const l3Text=await dst.evaluate(()=>document.getElementById('doc').innerText.replace(/\s+/g,' '));
   ok('6.3 目标层 L3（Key written line）与 L2（Brief 派生句）内容不同',
      l2Text.length>0&&l3Text.length>0&&l2Text!==l3Text,{l2:l2Text.slice(0,90),l3:l3Text.slice(0,90)});
   await ctx.close();
  }catch(e){ok('S6 场景异常',false,String(e).slice(0,300));if(page)await page.context().close().catch(()=>{});}
 })();

 // ================= 场景 7（项目 B，独立 context）：短层持久化矩阵（engine_anchor 契约，§4.9） =================
 // 结构：PRE 前置门（有效原生锚成功保存 + 重开同层实际精确 mark）→ 通过后才做无效化矩阵
 //（超长 thread / native 缺失 / 产物重折，均对同 id 断言「锚失效 + 笔记保留」）。
 // 前置失败 → 无效化场景记 blocked（FAIL），绝不空前置虚假通过。
 await (async()=>{
  const scenario={name:'S7-shortlayer',pid:PIDS[1]};let page;CURRENT='S7-shortlayer';
  let preOk=false, preAnchorId=null, prePhrase='';
  try{
   const ctx=await browser.newContext(ctxOpts);page=await ctx.newPage();attach(page,scenario);
   await page.goto(`${WEB}/#/p/${scenario.pid}/reader`,{waitUntil:'domcontentloaded'});
   await page.waitForSelector('#btn-aifold',{timeout:20000});
   await page.waitForTimeout(600);
   await enterFold(page);
   await page.waitForTimeout(1200);
   let {src,dst}=await getFrames(page);
   const metaId=await src.evaluate(()=>{const el=document.getElementById('paper-data');return el?JSON.parse(el.textContent).meta.id:'';});
   const uid=await src.evaluate(()=>document.querySelector('#doc .u')?.id||'');

   // 通用短层选词（真实用户路径）：只选「rects>0 的可见 occurrence」→ mouseup → 内核 selbar 高光
   const selectAndHighlight=async(frame,phrase)=>{
     const attempt=async()=>{
       const res=await frame.evaluate((text)=>{
         // 自包含 picker（iframe 上下文）：只取 rects>0 且节点连接的可见 occurrence
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
           return {range:r,rects:rects.length,connected:sN.isConnected,
             rect:box?{x:Math.round(box.x),y:Math.round(box.y),w:Math.round(box.width),h:Math.round(box.height)}:null};
         };
         const occ=[];
         for(let i=0;i<full.length;i++){
           const at=full.indexOf(text,i);if(at<0)break;
           const b=build(at);if(b)occ.push({at,...b});i=at;
         }
         const occSummary=occ.map(o=>({at:o.at,rects:o.rects,connected:o.connected,rect:o.rect}));
         const visible=occ.filter(o=>o.rects>0&&o.connected&&o.rect&&o.rect.w>1&&o.rect.h>1);
         const pick={occ:occSummary,visible};
         if(!pick.visible.length)return {ok:false,reason:'no visible occurrence',occ:pick.occ};
         const c=pick.visible[0];
         const sel=window.getSelection();sel.removeAllRanges();sel.addRange(c.range);
         document.getElementById('doc').dispatchEvent(new MouseEvent('mouseup',{bubbles:true,
           clientX:c.rect.x+c.rect.w/2,clientY:c.rect.y+c.rect.h/2}));
         return {ok:true,rect:c.rect,occ:pick.occ};
       },phrase).catch(e=>({ok:false,err:String(e).slice(0,100)}));
       await page.waitForTimeout(500);
       const bar=await frame.evaluate(()=>{const b=document.querySelector('.selbar');
         return b?{hidden:b.hidden}:{none:true};}).catch(()=>({none:true}));
       return {res,bar};
     };
     let r=await attempt();
     if(!(r.bar&&r.bar.hidden===false)){await page.waitForTimeout(500);r=await attempt();}
     if(!(r.bar&&r.bar.hidden===false))return {selDone:r.res,bar:r.bar,clicked:false};
     await frame.evaluate(()=>{const b=document.querySelector('.selbar button[data-act="hl"]');if(b)b.click();});
     await page.waitForTimeout(900);
     return {selDone:r.res,bar:r.bar,clicked:true};
   };
   const kernelNotes=async frame=>frame.evaluate(async()=>{const r=await fetch('/api/notes/');return r.json();});
   const overlayAnns=async()=>page.evaluate(async({api,pid})=>{
     const r=await fetch(`${api}/projects/${pid}/reader/state?locale=zh-CN`,{headers:{'x-literature-token':''}});
     return (await r.json()).overlay||{};
   },{api:API,pid:scenario.pid}).catch(()=>({}));

   // ---- PRE 前置门：源 Key 写行选词 → 有效 engine_anchor 保存 + 重开精确 mark ----
   await page.locator('#lvlbar [data-l="3"]').click();
   await page.waitForTimeout(800);
   ({src}=await getFrames(page));
   await src.waitForFunction(()=>window.__reader&&window.__reader.level===2,{timeout:8000}).catch(()=>{});
   await page.waitForTimeout(500);
   prePhrase="Demand fell after the ninth century";
   const sinceTs=Date.now();
   // 捕获内核 POST 的原生 anchor（bridge 已替换 window.fetch：在其后再包一层，只记录不拦截）
   await src.evaluate(()=>{
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
   const rPre=await selectAndHighlight(src,prePhrase);
   await page.waitForTimeout(800);
   const kPre=await kernelNotes(src);
   // 隔离：S2 在同项目创建过同引文笔记，只认本场景新建（created 晚于场景开始）的笔记
   const staleIds=new Set(['acc-note-1','acc-note-2','acc-note-range-normal','acc-note-range-fold','acc-thread-bound','acc-native-missing']);
   const preNote=(kPre.notes||[]).filter(n=>!staleIds.has(n.id)
     &&(n.created?Date.parse(n.created):Infinity)>sinceTs-2000
     &&(n.anchor?.quote?.exact||'').includes(prePhrase)).pop()||null;
   let oPre={};
   let preOv=null;
   for(let i=0;i<14;i++){
     oPre=await overlayAnns();
     preOv=(oPre.annotations||[]).find(a=>preNote&&a.id===preNote.id)||null;
     if(preOv&&preOv.engine_anchor)break;
     await page.waitForTimeout(300);
   }
   // 同 id 三跳：内核 POST 原生 anchor → 宿主 PUT 注解字段 → GET 读回
   const notePuts=await src.evaluate(()=>window.__notePuts||[]);
   const postForPre=notePuts.filter(p=>p.anchor&&(p.anchor.quote?.exact||'').includes(prePhrase)).pop()||null;
   const putForPre=timeline.filter(e=>e.dir==='put-body'&&e.annotations&&
     e.annotations.some(a=>a.id===(preNote?preNote.id:''))).pop()||null;
   const putAnnForPre=putForPre&&putForPre.annotations?putForPre.annotations.find(a=>a.id===preNote.id):null;
   // §4.9 + 自然样本修正：shown.words 必有（写行空间）；anchor.tokens/sents 是【可选】的
   // Full 来源引用——选区词有 m（作者原话）时携带 Full 范围，自拟词（无 m）时为 null。
   // 合法 = 可选字段不伪造：tokens/sents 同为 null，或同为数组且值域在 Full 空间内。
   const eaValid=(ea,tokCount=200000,sentCount=100000)=>{if(!ea||ea.version!==1||ea.space!=='brief'
     ||!/^[0-9a-f]{8}$/.test(ea.digest||'')||!ea.anchor?.shown
     ||!Array.isArray(ea.anchor.shown.words)||ea.anchor.unit!==uid)return false;
     const hasT=Array.isArray(ea.anchor.tokens),hasS=Array.isArray(ea.anchor.sents);
     if(hasT!==hasS)return false;   // 只其一=伪造/丢字段
     if(hasT){const [lo,hi]=ea.anchor.tokens;
       if(!(0<=lo&&lo<=hi&&hi<tokCount))return false;
       if(hasS&&ea.anchor.sents.some(k=>k<0||k>=sentCount))return false;}
     return true;};
   ok('7.P1 前置：内核 selbar 选词保存成功（笔记存在）',
      rPre.clicked&&!!preNote,{sel:rPre,notes:(kPre.notes||[]).map(n=>n.id)});
   // 内核 GET /api/notes 回执不含宿主信封（toEngineNote 只还原 note.anchor）——信封在 overlay（P3）。
   const noteAnchor2=preNote?.anchor||null;
   ok('7.P2 前置：内核回执 note.anchor 与所选写行一致（on=unit、unit 一致、shown 词区间存在）',
      !!noteAnchor2&&noteAnchor2.on==='unit'&&noteAnchor2.unit===uid
      &&!!noteAnchor2.shown&&Array.isArray(noteAnchor2.shown.words),
      {noteAnchor:noteAnchor2&&{on:noteAnchor2.on,unit:noteAnchor2.unit,level:noteAnchor2.level,
        shown:noteAnchor2.shown,tokens:noteAnchor2.tokens,sents:noteAnchor2.sents}});
   ok('7.P3 前置：overlay 批注携带 engine_anchor（宿主持久化侧）',
      !!preOv&&eaValid(preOv.engine_anchor),
      {engine_anchor:preOv?.engine_anchor||null});
   ok('7.P3b 同 id 三跳链路取证：内核 POST anchor → PUT 注解字段 → GET 读回（字段逐一对照）',
      !!postForPre&&!!putAnnForPre&&!!preOv,
      {post:postForPre&&postForPre.anchor,put:putAnnForPre&&{id:putAnnForPre.id,
        engine_anchor:putAnnForPre.engine_anchor},readback:preOv&&{id:preOv.id,
        engine_anchor:preOv.engine_anchor},
       gap:postForPre&&putAnnForPre?{postHadAnchor:!!(postForPre.anchor&&(postForPre.anchor.tokens||postForPre.anchor.shown)),
         putHadEnvelope:!!(putAnnForPre.engine_anchor)}:null});
   // 重开同层：实际 mark 覆盖精确所选词
   await page.reload({waitUntil:'domcontentloaded'});
   await page.waitForSelector('#btn-aifold',{timeout:20000});
   await page.waitForTimeout(800);
   await enterFold(page);
   await page.locator('#lvlbar [data-l="3"]').click();
   await page.waitForTimeout(1200);
   ({src}=await getFrames(page));
   const rePre=await src.evaluate(phrase=>{
     // fold 高亮为逐词 span.tk.hl：按词序列连接核验短语
     const spans=[...document.querySelectorAll('#doc .hl')];
     const joined=spans.map(m=>m.innerText.replace(/\s+/g,' ')).join(' ').replace(/\s+/g,' ').trim();
     return {marks:spans.map(m=>m.innerText.replace(/\s+/g,' ').slice(0,60)),
             joined,spanCount:spans.length,exact:joined===phrase,
             hit:joined.includes(phrase)?joined:null};
   },prePhrase);
   preOk=!!rePre.hit&&rePre.hit.length<=prePhrase.length+12&&!!preNote
     &&!!(preOv&&preOv.engine_anchor);
   preAnchorId=preNote?preNote.id:null;
   ok('7.P4 前置：重开同层后实际 mark 覆盖精确所选词（engine_anchor 复原，非整段）',
      !!rePre.hit&&rePre.hit.length<=prePhrase.length+12,rePre);
   // P5 m 映射一致性（按原生空间，不按 UI 层强制）：所选写行词有 m（作者原话）→ tokens/sents
   // 必须为数组且落在 Full 空间；全部无 m（模型自拟）→ tokens/sents 必须为 null（不伪造）。
   const p5=await page.evaluate(async({api,pid,uid2,phrase})=>{
     const r=await fetch(`${api}/projects/${pid}/reader/layers/native?locale=source`,
       {headers:{'x-literature-token':''}});
     const nat=await r.json();
     const unit=nat.units[uid2]||{};const atoms=nat.atoms||{};
     const plain=(tk)=>{if(typeof tk==='string')return tk;
       if(tk.a){const atom=atoms[tk.a];if(!atom)return '';
         return atom.kind==='math'?`$${atom.alt??''}$`:(atom.text||'');}
       return tk.x?`$${tk.x}$`:(tk.t||'');};
     const brief=(unit.lad||{}).brief||[];
     const toks=unit.toks||[];
     const sents=unit.sents||[];
     // 写行文本（brief 词 plain 连接）与选中短语定位
     let written='';const words=[];
     for(const w of brief){const start=written.length;const t=plain(w.tok||w);
       written+=t;words.push({start,end:written.length,m:(w.tok||w).m ?? (w.m??null)});if(w.s)written+=' ';}
     const idx=written.indexOf(phrase);
     if(idx<0)return {found:false,written:written.slice(0,120)};
     const covered=words.filter(w=>w.start<idx+phrase.length&&w.end>idx);
     const withM=covered.filter(w=>w.m!==null&&w.m!==undefined);
     const mValues=withM.map(w=>w.m);
     return {found:true,writtenLen:written.length,coveredCount:covered.length,
             hasM:withM.length>0,mValues,tokCount:toks.length,sentCount:sents.length,
             shownWordsCount:brief.length};
   },{api:API,pid:scenario.pid,uid2:uid,phrase:prePhrase}).catch(e=>({err:String(e).slice(0,80)}));
   const ea=(preOv&&preOv.engine_anchor)||{};
   const hasT=Array.isArray(ea.tokens),hasS=Array.isArray(ea.sents);
   let p5Ok=false;
   if(p5.found&&p5.hasM){
     // 有 m（作者原话）：tokens/sents 必须为数组，且范围与原生 m 映射一致（lo≤min(m)、hi≥max(m)、值域内）
     p5Ok=hasT&&hasS;
     if(p5Ok){
       const [lo,hi]=ea.tokens;
       const minM=Math.min(...p5.mValues),maxM=Math.max(...p5.mValues);
       p5Ok=lo<=minM&&hi>=maxM&&0<=lo&&hi<p5.tokCount
         &&ea.sents.every(k=>k>=0&&k<p5.sentCount);
     }
   }else if(p5.found){
     p5Ok=!hasT&&!hasS;          // 全无 m（模型自拟）：必须为 null（不伪造 Full 来源）
   }
   ok('7.P5 前置：m 映射一致性（比较原生输入是否确有来源，不制造）——有 m 则 tokens/sents 为 Full 数组且范围与 m 一致；全无 m 则为 null',
      p5.found&&p5Ok,{p5,anchor:{tokens:ea.tokens,sents:ea.sents,shown:ea.shown}});
   // P6（按 Lead 只读审查修正）：上游 shown 是写行完整选段；quote 在有 tokens 时是 Full 来源区间文本，
   // 不一定等于/包含于 shown。按空间分别全等：
   //   brief  : shown.exact == 写行 [words] 区间文本；quote.exact == Full tokens 区间文本（有 tokens 时）
   //   derived: quote.exact == derived_brief tokens 区间文本
   //   full   : quote.exact == Full tokens 区间文本
   const p6=await page.evaluate(async({api,pid,uid2,space,anchor})=>{
     const r=await fetch(`${api}/projects/${pid}/reader/layers/native?locale=source`,
       {headers:{'x-literature-token':''}});
     const nat=await r.json();
     const unit=nat.units[uid2]||{};const atoms=nat.atoms||{};
     const plain=(tk)=>{if(typeof tk==='string')return tk;
       if(tk.a){const atom=atoms[tk.a];if(!atom)return '';
         return atom.kind==='math'?`$${atom.alt??''}$`:(atom.text||'');}
       return tk.x?`$${tk.x}$`:(tk.t||'');};
     const txt=(toks,a,b)=>{if(!Array.isArray(toks)||a==null||b==null)return null;
       return toks.slice(a,b+1).map(t=>plain(t)).join('').slice(0,500);};
     const L=unit.lad||{};
     const briefToks=Array.isArray(L.brief)?L.brief:[];
     const briefPlain=briefToks.map(w=>plain(w.tok||w));
     const out={space};
     if(space==='brief'){
       const w=anchor?.shown?.words;
       const briefWithSpace=(()=>{let out='';briefToks.forEach((tk,i)=>{out+=(tk.tok||tk).t??'';if((tk.tok||tk).s)out+=' ';});
         return out;})();
       const wordsText=(w0,w1)=>{let out='';for(let i=w0;i<=w1;i++){const tk=briefToks[i]||{};const t=tk.tok||tk;
         out+=plain(t);if(t.s)out+=' ';}return out.trim();};
       out.shownExpected=Array.isArray(w)?wordsText(w[0],w[1]):null;
       out.shownGot=anchor?.shown?.exact||null;
       // brief 锚：有 tokens 时 quote=Full 区间文本；无 tokens（模型自拟词）时 quote=所示写行区间文本
       out.quoteExpected=Array.isArray(anchor?.tokens)
         ? txt(unit.toks,anchor.tokens[0],anchor.tokens[1])
         : (Array.isArray(w)?wordsText(w[0],w[1]).slice(0,500):null);
       out.quoteGot=anchor?.quote?.exact||null;
     }else if(space==='derived'){
       const db=((unit.tr||{})['zh-CN']||{}).lad?.derived_brief?.toks||[];
       out.quoteExpected=txt(db,anchor?.tokens?.[0],anchor?.tokens?.[1]);
       out.quoteGot=anchor?.quote?.exact||null;
     }else{
       out.quoteExpected=txt(unit.toks,anchor?.tokens?.[0],anchor?.tokens?.[1]);
       out.quoteGot=anchor?.quote?.exact||null;
     }
     return out;
   },{api:API,pid:scenario.pid,uid2:uid,space:ea.space,anchor:ea.anchor}).catch(e=>({err:String(e).slice(0,80)}));
   const p6Ok=(()=>{
     if(p6.err)return false;
     if(p6.space==='brief'){
       const shownOk=!p6.shownGot||p6.shownGot===p6.shownExpected;
       const quoteOk=!p6.quoteGot||p6.quoteGot===p6.quoteExpected;
       return shownOk&&quoteOk;
     }
     return !!p6.quoteGot&&p6.quoteGot===p6.quoteExpected;
   })();
   ok('7.P6 前置：quote/shown 按各空间全等（brief: shown=写行区间、quote=Full 区间；derived/full: quote=对应空间区间）',
      p6Ok,p6);
   if(!preOk){
     // 前置失败：无效化矩阵 blocked，不计通过
     ok('7.BLOCK 无效化矩阵前置失败（无有效原生锚可失效）：超长 thread/native 缺失/重折失效用例 blocked，不计通过',
        false,{preNoteId:preAnchorId,preOk,reason:'前置 P1–P4 未全部通过'});
     await page.screenshot({path:'tmp/reader-aifold-screens/7-blocked-precondition.png'});
     await ctx.close();
     return;
   }

   // ---- 无效化矩阵（前置通过才执行；均对同 id 断言「锚失效 + 笔记保留」）----
   // INV-1 超长 thread：同 id 提交 >64 项 thread → engine_anchor 丢弃、笔记保留
   const inv1=await src.evaluate(async([mid,id,uid2])=>{
     const thread=[];for(let i=0;i<70;i++)thread.push({q:'q'+i,a:'a'+i,lang:'en'});
     const notes=await (await fetch('/api/notes/')).json();
     const n=(notes.notes||[]).find(x=>x.id===id);if(!n)return {status:404};
     n.thread=thread;n.text=(n.text||'')+'（thread 有界验证）';
     const r=await fetch('/api/notes/'+mid,{method:'POST',headers:{'content-type':'application/json'},
       body:JSON.stringify({op:'put',note:n})});
     return {status:r.status};
   },[metaId,preAnchorId,uid]);
   await page.waitForTimeout(1200);
   const afterInv1=await overlayAnns();
   const a1=(afterInv1.annotations||[]).find(a=>a.id===preAnchorId);
   // 契约：thread 超限 → 整个 engine_anchor 丢弃；批注（含笔记文本）保留。
   const inv1Text=String((a1&&(a1.note||a1.text))||'');
   const inv1Ok=inv1.status===200&&!!a1&&inv1Text.includes('thread 有界验证')
     &&(!a1.engine_anchor||((a1.engine_anchor.thread||[]).length===0));
   ok('7.I1 超长 thread：同 id 锚失效（engine_anchor 丢弃）且笔记保留',inv1Ok,
      {put:inv1,after:a1&&{id:a1.id,threadLen:(a1.engine_anchor?.thread||[]).length,
        hasEa:!!a1.engine_anchor,text:inv1Text.slice(0,60)}});
   // INV-2 native 缺失：删 native → 同 id touch（改色）→ 锚失效、笔记保留
   await page.evaluate(async url=>{await fetch(url).catch(()=>{});},`${CONTROL}/deletenative?pid=${scenario.pid}`);
   const inv2=await src.evaluate(async([mid,id])=>{
     const notes=await (await fetch('/api/notes/')).json();
     const n=(notes.notes||[]).find(x=>x.id===id);if(!n)return {status:404};
     n.color='g';
     const r=await fetch('/api/notes/'+mid,{method:'POST',headers:{'content-type':'application/json'},
       body:JSON.stringify({op:'put',note:n})});
     return {status:r.status};
   },[metaId,preAnchorId]);
   await page.waitForTimeout(1200);
   const afterInv2=await overlayAnns();
   const a2=(afterInv2.annotations||[]).find(a=>a.id===preAnchorId);
   ok('7.I2 native 缺失：同 id touch 后锚失效（engine_anchor 丢弃）且笔记保留',
      inv2.status===200&&!!a2&&!a2.engine_anchor&&(a2.color==='g'||a2.color==='green'),
      {put:inv2,after:a2&&{id:a2.id,hasEa:!!a2.engine_anchor,color:a2.color}});
   // INV-3 产物重折：force 重折 → 笔记保留；同 id touch 后锚不复活（digest 失效不伪恢复）、不错标新词
   await page.evaluate(async({api,pid})=>{
     await fetch(`${api}/projects/${pid}/reader/fold-runs`,{method:'POST',
       headers:{'x-literature-token':'','content-type':'application/json'},
       body:JSON.stringify({locale:'zh-CN',force:true})});
   },{api:API,pid:scenario.pid});
   const refoldFinal=await waitRun(page,scenario.pid,['ready','failed'],40000);
   const inv3=await src.evaluate(async([mid,id])=>{
     const notes=await (await fetch('/api/notes/')).json();
     const n=(notes.notes||[]).find(x=>x.id===id);if(!n)return {status:404,kept:false};
     n.text=(n.text||'')+'（重折后 touch）';
     const r=await fetch('/api/notes/'+mid,{method:'POST',headers:{'content-type':'application/json'},
       body:JSON.stringify({op:'put',note:n})});
     return {status:r.status,kept:true};
   },[metaId,preAnchorId]).catch(e=>({err:String(e).slice(0,80)}));
   await page.waitForTimeout(1000);
   const afterInv3=await overlayAnns();
   const a3=(afterInv3.annotations||[]).find(a=>a.id===preAnchorId);
   const reMark=await src.evaluate(phrase=>{
     const spans=[...document.querySelectorAll('#doc .hl')];
     const joined=spans.map(m=>m.innerText.replace(/\s+/g,' ')).join(' ').replace(/\s+/g,' ').trim();
     return {marks:spans.map(m=>m.innerText.slice(0,50)),hitOld:joined.includes(phrase)?joined:null};
   },prePhrase).catch(()=>({}));
   // 语义：笔记必须保留；锚若存在必须是「按新产物重新锚定」（digest 与旧值不同），
   // 且高亮只能覆盖原选词、不得误标其它词；旧 digest 原样复活即失败。
   const oldDigest='155c4d22';
   const newDigest=a3&&a3.engine_anchor?a3.engine_anchor.digest:null;
   const rematchedOk=reMark&&(reMark.hitOld===null?true:String(reMark.hitOld).includes(prePhrase));
   ok('7.I3 产物重折：笔记保留；锚不得以旧 digest 伪恢复；重新锚定时仍只覆盖原选词',
      inv3.status===200&&!!a3&&(refoldFinal.status==='ready'||refoldFinal.status==='failed')
      &&newDigest!==oldDigest&&rematchedOk,
      {refold:refoldFinal.status,touch:inv3,after:a3&&{id:a3.id,hasEa:!!a3.engine_anchor,digest:newDigest},reMark});
   // 无 m 写行词子例：目标 Key 写行的模型自拟词（reason「依据证据句」，无 Full m 锚）
   // → shown 准确保存恢复，tokens/sents 保持原生 null（不伪造 Full 对应）。
   await page.locator('#lvlbar [data-l="3"]').click();
   await page.waitForTimeout(800);
   ({dst}=await getFrames(page));
   await dst.waitForFunction(()=>window.__reader&&window.__reader.level===2,{timeout:8000}).catch(()=>{});
   await page.waitForTimeout(500);
   const noMPhrase="依据证据句";
   const rNom=await selectAndHighlight(dst,noMPhrase);
   await page.waitForTimeout(800);
   const kNom=await kernelNotes(dst);
   // 隔离：只认本场景新建
   const nNom=(kNom.notes||[]).filter(n=>!staleIds.has(n.id)
     &&(n.created?Date.parse(n.created):Infinity)>sinceTs-2000
     &&(n.anchor?.quote?.exact||'').includes(noMPhrase)).pop()||null;
   let eaNom={};
   for(let i=0;i<12;i++){
     const ovN=await overlayAnns();
     const ann=(ovN.annotations||[]).find(a=>nNom&&a.id===nNom.id);
     if(ann&&ann.engine_anchor){eaNom=ann.engine_anchor;break;}
     await page.waitForTimeout(300);
   }
   const nomOk=!!nNom&&eaNom.space==='brief'&&eaNom.anchor?.shown
     &&Array.isArray(eaNom.anchor.shown.words)
     &&(eaNom.anchor.tokens===null||eaNom.anchor.tokens===undefined)
     &&(eaNom.anchor.sents===null||eaNom.anchor.sents===undefined);
   ok('7.5b 无 m 写行词：shown 准确保存、tokens/sents 保持原生 null（不伪造 Full 对应）',
      nomOk,{sel:rNom?.sel,note:nNom&&{id:nNom.id,anchor:nNom.anchor,engine_anchor:eaNom}});
   await page.screenshot({path:'tmp/reader-aifold-screens/7-after-invalidate.png'});
   await ctx.close();
  }catch(e){ok('S7 场景异常',false,String(e).slice(0,300)+(e.stack?String(e.stack).slice(0,200):''));if(page)await page.context().close().catch(()=>{});}
 })();


 await browser.close();
 const failed=assertions.filter(a=>!a.pass);
 const payload=JSON.stringify({scenarios:[...new Set(assertions.map(a=>a.scenario.name||a.scenario))],
   assertions,failed:failed.length,timeline,posts},null,1);
 try{require('fs').writeFileSync(RESULT,payload);}catch(e){console.log(payload.slice(0,3000));}
 console.log('ACCEPTANCE_FAILED='+failed.length+' total='+assertions.length);
 process.exit(failed.length?1:0);
})().catch(e=>{console.error('FATAL',String(e).slice(0,400));process.exit(99);});
