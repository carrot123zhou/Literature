const {chromium}=require(process.env.PW_PACKAGE_PATH);

// 最小稳定复现：问题 A —— engine_anchor 已落盘但刷新重开同层无 mark（单场景，源 Key unit 锚）。
// argv: [web, api, pid, resultPath]
const WEB=`http://127.0.0.1:${process.argv[2]}`;
const API=`http://127.0.0.1:${process.argv[3]}`;
const PID=process.argv[4];
const RESULT=process.argv[5];
const out={hops:{},reopen:{},fingerprintNote:null};

async function frameOf(page,pane,timeout=20000){
  const deadline=Date.now()+timeout;
  while(Date.now()<deadline){
    const h=await page.$(`#rdrr-pane-${pane} .rdrr-tfhost iframe`);
    if(h){const f=await h.contentFrame();
      if(f&&await f.evaluate(()=>!!window.__reader).catch(()=>false))return f;}
    await new Promise(r=>setTimeout(r,200));
  }
  return null;
}
const lv=async f=>f.evaluate(()=>({level:window.__reader?.level,lang:window.__reader?.lang,
  staged:!!window.__reader?.staged,pos:window.__reader?.pos})).catch(()=>({}));
async function setLevelBar(page,frames,productLevel){
  const want=5-productLevel;
  for(let i=0;i<3;i++){
    await page.locator(`#lvlbar [data-l="${productLevel}"]`).click();
    const deadline=Date.now()+3000;
    while(Date.now()<deadline){
      const s=await lv(frames.src),d=frames.dst?await lv(frames.dst):s;
      if(s.level===want&&d.level===want){await page.waitForTimeout(600);return {synced:true,src:s,dst:d};}
      await page.waitForTimeout(150);
    }
  }
  return {synced:false,src:await lv(frames.src),dst:frames.dst?await lv(frames.dst):null};
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
const putBodies=[];
const attach=page=>{
  page.on('request',r=>{
    if(r.method()==='PUT'&&r.url().includes('/reader/overlay')){
      try{const b=r.postDataJSON();
        putBodies.push({rev:b.base_revision,anns:(b.overlay?.annotations||[]).map(a=>({
          id:a.id,anchor:a.anchor,pane:a.pane,
          engine_anchor:a.engine_anchor?{space:a.engine_anchor.space,digest:a.engine_anchor.digest,
            anchor:a.engine_anchor.anchor}:null}))});}catch(e){}
    }
  });
};

// 诊断增强（不改断言）：在每个 frame（含 srcdoc iframe）脚本运行前拦截 litFetchBridge 赋值，
// 捕获 **boot 期** /api/notes GET 的实际回执与时间线；宿主侧记录 /reader/overlay GET 完成时刻。
const BOOT_HOOK = `
  window.__notesGets = window.__notesGets || [];
  try {
    let captured = null;
    Object.defineProperty(window, 'litFetchBridge', {
      configurable: true,
      set(fn) {
        captured = fn;
        Object.defineProperty(window, 'litFetchBridge', {
          configurable: true, writable: true, value: function(path, init) {
            const p = fn(path, init);
            if (String(path).includes('/api/notes')) {
              const t = Date.now();
              p.then(d => { try { window.__notesGets.push({ t, path: String(path).slice(0, 60),
                reply: JSON.parse(JSON.stringify(d)).slice ? d : d, notesCount: (d && d.notes ? d.notes.length : -1),
                first: d && d.notes && d.notes[0] ? {
                  level: d.notes[0].anchor && d.notes[0].anchor.level,
                  shown: d.notes[0].anchor && d.notes[0].anchor.shown ? d.notes[0].anchor.shown.words : null,
                  lang: d.notes[0].anchor && d.notes[0].anchor.lang,
                  unit: d.notes[0].anchor && d.notes[0].anchor.unit } : null })); } catch (e) {} });
            }
            return p;
          }
        });
      },
      get() { return captured; }
    });
  } catch (e) { window.__hookErr = String(e); }
`;
const HOST_HOOK = `
  window.__overlayGets = window.__overlayGets || [];
  const __origFetch = window.fetch.bind(window);
  window.fetch = function(input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const p = __origFetch(input, init);
    if (String(url).includes('/reader/overlay') || String(url).includes('/reader/state')) {
      const t = Date.now();
      p.then(r => r.clone().json().then(d => {
        try { window.__overlayGets.push({ t, url: String(url).slice(0, 80),
          annCount: (d.overlay && d.overlay.annotations ? d.overlay.annotations.length : (d.annotations ? d.annotations.length : -1)),
          firstEa: ((d.overlay && d.overlay.annotations ? d.overlay.annotations[0] : (d.annotations ? d.annotations[0] : null)) || {}).engine_anchor ? {
            space: (d.overlay.annotations ? d.overlay.annotations[0].engine_anchor.space : d.annotations[0].engine_anchor.space),
            digest: (d.overlay.annotations ? d.overlay.annotations[0].engine_anchor.digest : d.annotations[0].engine_anchor.digest) } : null });
        } catch (e) {}
      }).catch(() => {}));
    }
    return p;
  };
`;
// 真实文本 Range 选词（可指定容器选择器）：mouseup → 内核 selbar
async function selectIn(frame, rootSelector, phrase) {
  return frame.evaluate(({ rootSelector, phrase }) => {
    const root = rootSelector ? document.querySelector(rootSelector) : document.getElementById('doc');
    if (!root) return { ok: false, why: 'no root' };
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node; const parts = [];
    while (node = walker.nextNode()) parts.push({ node, text: node.textContent });
    const full = parts.map(p => p.text).join(''); const idx = full.indexOf(phrase);
    if (idx < 0) return { ok: false, why: 'phrase not found', full: full.replace(/\s+/g, ' ').slice(0, 160) };
    let acc = 0, sN = null, sO = 0, eN = null, eO = 0;
    for (const p of parts) { const len = p.text.length;
      if (sN === null && acc + len > idx) { sN = p.node; sO = idx - acc; }
      if (acc < idx + phrase.length) { eN = p.node; eO = idx + phrase.length - acc; }
      acc += len; if (sN && eN && acc >= idx + phrase.length) break; }
    const range = document.createRange(); range.setStart(sN, sO); range.setEnd(eN, eO);
    const s = window.getSelection(); s.removeAllRanges(); s.addRange(range);
    const r = range.getBoundingClientRect();
    root.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2 }));
    return { ok: true };
  }, { rootSelector, phrase });
}
(async()=>{
 const browser=await chromium.launch({headless:true});
 const ctx=await browser.newContext({viewport:{width:1440,height:920}});
 await ctx.addInitScript(BOOT_HOOK);       // 所有 frame 生效（含 srcdoc iframe）
 await ctx.addInitScript(HOST_HOOK);       // 宿主侧 overlay GET 时间线
 const page=await ctx.newPage();page.setDefaultTimeout(20000);attach(page);
 const phrase='Demand fell after the ninth century';
 try{
  await page.goto(`${WEB}/#/p/${PID}/reader`,{waitUntil:'domcontentloaded'});
  await page.waitForSelector('#btn-aifold',{timeout:20000});
  await page.waitForTimeout(600);
  await enterFold(page);
  await page.waitForTimeout(1200);
  const src=await frameOf(page,'source'),dst=await frameOf(page,'trans');
  const sync=await setLevelBar(page,{src,dst},3);   // 产品 L3 = 引擎 2（Key 写行）
  out.hops.barSync=sync;
  out.hops.iframeBefore=await src.evaluate(()=>({level:window.__reader.level,lang:window.__reader.lang,
    staged:!!window.__reader.staged,text:(document.getElementById('doc').innerText||'').replace(/\s+/g,' ').slice(0,120)}));
  // 只记录（不改输入/断言）：捕获 iframe 实际收到的宿主 RPC 回执，区分「宿主返回」与「存盘」
  await src.evaluate(()=>{
    if(window.__rpcInstalled)return;window.__rpcInstalled=true;
    window.__rpcLog=[];
    const prevBridge=window.litFetchBridge;
    if(typeof prevBridge==='function'){
      window.litFetchBridge=function(path,init){
        const rec={t:Date.now(),path:String(path).slice(0,60),
          method:(init&&init.method)||'GET',body:null,reply:null,error:null};
        try{rec.body=init&&init.body?JSON.parse(init.body):null;}catch(e){rec.body=init&&init.body||null;}
        window.__rpcLog.push(rec);
        const p=prevBridge(path,init);
        p.then(d=>{rec.reply=d;},e=>{rec.error=String((e&&e.message)||e);});
        return p;   // 原样返回，行为不变
      };
    }
  });
  // 捕获内核 POST 原生 anchor
  await src.evaluate(()=>{
    if(window.__capInstalled)return;window.__capInstalled=true;
    const prev=window.fetch;window.__notePuts=[];
    window.fetch=function(i,init){
      try{const u=typeof i==='string'?i:(i&&i.url)||'';
        if(String(u).includes('/api/notes/')&&init&&init.body){
          const b=JSON.parse(init.body);
          if(b.op==='put')window.__notePuts.push({t:Date.now(),id:b.note&&b.note.id,anchor:b.note&&b.note.anchor});}
      }catch(e){}
      return prev(i,init);
    };
  });
  // 真实文本 Range 选词 → mouseup → selbar → 高亮
  const sel=await src.evaluate(text=>{
    const doc=document.getElementById('doc');
    const walker=document.createTreeWalker(doc,NodeFilter.SHOW_TEXT);
    let node;const parts=[];
    while(node=walker.nextNode())parts.push({node,text:node.textContent});
    const full=parts.map(p=>p.text).join('');const idx=full.indexOf(text);
    if(idx<0)return {ok:false,full:full.slice(0,120)};
    let acc=0,sN=null,sO=0,eN=null,eO=0;
    for(const p of parts){const len=p.text.length;
      if(sN===null&&acc+len>idx){sN=p.node;sO=idx-acc;}
      if(acc<idx+text.length){eN=p.node;eO=idx+text.length-acc;}
      acc+=len;if(sN&&eN&&acc>=idx+text.length)break;}
    const range=document.createRange();range.setStart(sN,sO);range.setEnd(eN,eO);
    const s=window.getSelection();s.removeAllRanges();s.addRange(range);
    const r=range.getBoundingClientRect();
    doc.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,clientX:r.x+r.width/2,clientY:r.y+r.height/2}));
    return {ok:true};
  },phrase);
  await page.waitForTimeout(1000);
  const bar=await src.evaluate(()=>({hidden:document.querySelector('.selbar')?.hidden,
    buttons:[...(document.querySelector('.selbar')?.querySelectorAll('button')||[])].map(b=>b.dataset.act)}));
  out.hops.selection=sel;out.hops.selbar=bar;
  if(bar.hidden){out.error='selbar hidden';throw new Error('selbar hidden');}
  await src.evaluate(()=>document.querySelector('.selbar button[data-act="hl"]').click());
  await page.waitForTimeout(1200);
  // 同 id 三跳
  const puts=await src.evaluate(()=>window.__notePuts||[]);
  const kernel=await src.evaluate(async()=>(await (await fetch('/api/notes/')).json()));
  const mine=(kernel.notes||[]).filter(n=>(n.anchor?.quote?.exact||'').includes(phrase)).pop()||null;
  const ov=await page.evaluate(async({api,pid})=>{
    const r=await fetch(`${api}/projects/${pid}/reader/state?locale=zh-CN`,{headers:{'x-literature-token':''}});
    return (await r.json()).overlay||{};
  },{api:API,pid:PID});
  const ovAnn=(ov.annotations||[]).find(a=>(a.quote?.exact||'').includes(phrase))||null;
  const put=(putBodies.slice().reverse().find(b=>b.anns.some(a=>a.id===(mine?.id||'##')))||null);
  const putAnn=put&&put.anns.find(a=>a.id===mine?.id);
  out.hops.kernelPost=puts.find(p=>p.id===mine?.id)||puts[puts.length-1]||null;
  out.hops.hostPut=putAnn||null;
  out.hops.getReadback=ovAnn?{id:ovAnn.id,anchor:ovAnn.anchor,pane:ovAnn.pane,
    engine_anchor:ovAnn.engine_anchor||null}:null;
  out.hops.noteId=mine?.id||null;
  out.hops.rpcLogAfterSave=await src.evaluate(()=>window.__rpcLog||[]).catch(()=>[]);
  // —— 章节标题（on=title）场景：hd 标题选词 → hl → PUT（space=title，无 tokens）——
  const titleSel=await selectIn(src,'#S1 .hd','Introduction');
  await page.waitForTimeout(1000);
  const titleBar=await src.evaluate(()=>({hidden:document.querySelector('.selbar')?.hidden}));
  out.title={selection:titleSel,selbar:titleBar};
  if(!titleBar.hidden){
    await src.evaluate(()=>document.querySelector('.selbar button[data-act="hl"]').click());
    await page.waitForTimeout(1200);
    const titlePuts=(await src.evaluate(()=>window.__notePuts||[])).filter(p=>p.anchor&&p.anchor.on==='title');
    out.title.kernelPost=titlePuts[titlePuts.length-1]||null;
  }
  // —— 章节摘要（on=summary）场景：S1 stake 写行选词 → hl → PUT（space=summary）——
  const sumPhrase='Silver vessels travelled';
  const sumSel=await selectIn(src,'#S1 .stake',sumPhrase);
  await page.waitForTimeout(1000);
  const sumBar=await src.evaluate(()=>({hidden:document.querySelector('.selbar')?.hidden,
    buttons:[...(document.querySelector('.selbar')?.querySelectorAll('button')||[])].map(b=>b.dataset.act)}));
  out.summary={selection:sumSel,selbar:sumBar};
  if(sumBar.hidden){out.summary.error='selbar hidden';}
  else{
    await src.evaluate(()=>document.querySelector('.selbar button[data-act="hl"]').click());
    await page.waitForTimeout(1200);
    const sumPuts=(await src.evaluate(()=>window.__notePuts||[])).filter(p=>p.anchor&&p.anchor.on==='summary');
    const sumKernelPost=sumPuts.find(p=>(p.anchor.quote&&p.anchor.quote.exact||'').includes(sumPhrase))||sumPuts[sumPuts.length-1]||null;
    out.summary.kernelPost=sumKernelPost;
    const ovAfterSum=await page.evaluate(async({api,pid})=>{
      const r=await fetch(`${api}/projects/${pid}/reader/state?locale=zh-CN`,{headers:{'x-literature-token':''}});
      return (await r.json()).overlay||{};
    },{api:API,pid:PID});
    const ovSum=(ovAfterSum.annotations||[]).find(a=>a.engine_anchor&&a.engine_anchor.space==='summary')||null;
    out.summary.hostPut=ovSum?{space:ovSum.engine_anchor.space,digest:ovSum.engine_anchor.digest,
      locale:ovSum.engine_anchor.locale,block_id:ovSum.block_id,
      anchor:ovSum.engine_anchor.anchor}:null;
  }
  // 字段级对比（A 的核心）
  const F=a=>a?{space:a.space,digest:a.digest,unit:a.anchor?.unit,level:a.anchor?.level,
    tokens:a.anchor?.tokens,sents:a.anchor?.sents,shown:a.anchor?.shown?.words,
    quote:(a.anchor?.quote?.exact||'').slice(0,60)}:null;
  out.fieldDiff={
    kernelPost:F(out.hops.kernelPost&&{space:'(posted)',anchor:out.hops.kernelPost.anchor}),
    hostPut:F(out.hops.hostPut&&{space:out.hops.hostPut.engine_anchor?.space,
      digest:out.hops.hostPut.engine_anchor?.digest,anchor:out.hops.hostPut.engine_anchor?.anchor}),
    getReadback:F(out.hops.getReadback&&{space:out.hops.getReadback.engine_anchor?.space,
      digest:out.hops.getReadback.engine_anchor?.digest,anchor:out.hops.getReadback.engine_anchor?.anchor}),
  };
  // 刷新 → 重进 → 同层：marks
  await page.reload({waitUntil:'domcontentloaded'});
  await page.waitForSelector('#btn-aifold',{timeout:20000});
  await page.waitForTimeout(900);
  await enterFold(page);
  const src2=await frameOf(page,'source'),dst2=await frameOf(page,'trans');
  const sync2=await setLevelBar(page,{src:src2,dst:dst2},3);
  await page.waitForTimeout(1200);
  const eaDebug=await src2.evaluate(()=>{try{const r=window.__reader;return {hasReader:!!r,keys:r?Object.keys(r):null,eaType:r?typeof r.__eaDebug:null,ea:r&&r.__eaDebug?r.__eaDebug():null}}catch(e){return{err:String(e).slice(0,200)}}}).catch(e=>({err:String(e).slice(0,200)}));
  out.reopen.eaDebug=eaDebug;
  const after=await src2.evaluate(phrase=>{
    // 内核 fold 视图的高亮是 span.tk.hl（fill() 设 className），不是 <mark>（那是非 fold 阅读视图的形态）
    const marks=[...document.querySelectorAll('#doc .hl')];
    const unit=document.getElementById('b-p-1');
    return {level:window.__reader.level,lang:window.__reader.lang,
      marks:marks.map(m=>m.innerText.replace(/\s+/g,' ').slice(0,60)),
      // 高亮是逐词 span.tk.hl：断言按词序列连接后匹配短语（不是单元素含整短语）
      hit:marks.map(m=>m.innerText.replace(/\s+/g,' ')).join(' ').replace(/\s+/g,' ').includes(phrase),
      markSample:marks.map(m=>({tag:m.tagName,cls:m.className.slice(0,40),text:m.innerText.replace(/\s+/g,' ').slice(0,60)})).slice(0,8),
      noteText:(document.getElementById('doc').innerText||'').includes('Demand fell'),
      // 诊断：内核 notesOf 是否有该 unit 的笔记（notesChanged 会打 has-notes 类与 --note-bar）
      unitHasNotes:unit?unit.classList.contains('has-notes'):null,
      unitNoteBar:unit?getComputedStyle(unit).getPropertyValue('--note-bar'):null,
      // 渲染模式诊断：tok 模式有逐词 .tk 片段（line marks 可附着）；text 模式无片段
      unitMode:unit?unit._mode||null:null,
      unitStale:unit?!!unit._stale:null,
      tkSpans:document.querySelectorAll('#b-p-1 .tk').length,
      unitText:(unit&&unit._tx?unit._tx.textContent:(unit?unit.textContent:'')).replace(/\s+/g,' ').slice(0,160),
      allUnitModes:[...document.querySelectorAll('#doc .u')].slice(0,4).map(u=>({id:u.id,mode:u._mode||null,stale:!!u._stale,tks:u.querySelectorAll('.tk').length})),
      bootNotesGets:window.__notesGets||[],hookErr:window.__hookErr||null,
      ea:(()=>{try{const r=window.__reader;return {keys:r?Object.keys(r).slice(0,20):null,
        ea:(r&&r.__eaDebug)?r.__eaDebug():{missing:r?typeof r.__eaDebug:'no-reader'}};}catch(e){return{err:String(e).slice(0,200)}}})()};
  },phrase).catch(e=>({err:String(e).slice(0,80)}));
  out.reopen={barSync:sync2,after};
  out.reopen.hostOverlayGets=await page.evaluate(()=>window.__overlayGets||[]).catch(()=>[]);
  // 重开时 iframe 收到的 GET 回执（是否携带 engine_anchor —— 宿主返回 vs 存盘）
  // 新 iframe 是全新文档：重装 hook 并主动重拉一次 GET（标注 post-boot，boot 时那一次无法回收）
  const hookSrc=()=>{
    if(window.__rpcInstalled)return;window.__rpcInstalled=true;
    window.__rpcLog=[];
    const prevBridge=window.litFetchBridge;
    if(typeof prevBridge!=='function')return;
    window.litFetchBridge=function(path,init){
      const rec={t:Date.now(),path:String(path).slice(0,60),method:(init&&init.method)||'GET',
        body:null,reply:null,error:null};
      try{rec.body=init&&init.body?JSON.parse(init.body):null;}catch(e){rec.body=init&&init.body||null;}
      window.__rpcLog.push(rec);
      const p=prevBridge(path,init);
      p.then(d=>{rec.reply=d;},e=>{rec.error=String((e&&e.message)||e);});
      return p;
    };
  };
  await src2.evaluate(hookSrc).catch(()=>{});
  const refetch=await src2.evaluate(async()=>{
    try{
      const r=await fetch('/api/notes/');
      const d=await r.json();
      // 内核 GET 回执本来就不带宿主 engine_anchor 信封（toEngineNote 已把信封内原生 anchor 还原为 note.anchor）
      return {status:r.status,notes:(d.notes||[]).map(n=>({id:n.id,text:(n.text||'').slice(0,40),
        anchor:n.anchor?{on:n.anchor.on,unit:n.anchor.unit,section:n.anchor.section,
          lang:n.anchor.lang,level:n.anchor.level,tokens:n.anchor.tokens,sents:n.anchor.sents,
          shown:n.anchor.shown,quote:(n.anchor.quote&&n.anchor.quote.exact||'').slice(0,40)}:null}))};
    }catch(e){return {error:String(e).slice(0,120)};}
  }).catch(e=>({error:String(e).slice(0,120)}));
  // 语义声明：这是 post-boot 额外 GET，只证明该时刻宿主会返回什么；
  // 不能证明 boot 时 loadNotes 消费了同一份数据，也未被用来驱动恢复（不掩初始化时序 bug）。
  out.reopen.kernelRefetchReply={semantics:'post-boot extra GET (not boot loadNotes); informational only',...refetch};
  // GET 再次确认锚仍在（先取 overlay，供逐字段对比与持久化断言）
  const ov2=await page.evaluate(async({api,pid})=>{
    const r=await fetch(`${api}/projects/${pid}/reader/state?locale=zh-CN`,{headers:{'x-literature-token':''}});
    return (await r.json()).overlay||{};
  },{api:API,pid:PID});
  out.reopen.annotationStillStored=!!(ov2.annotations||[]).find(a=>a.id===out.hops.noteId);
  out.reopen.storedAnchor=ov2.annotations?.find(a=>a.id===out.hops.noteId)?.engine_anchor?.digest||null;
  // 章节 summary 恢复：stake 写行 marks（span.hl）+ annotation 存续
  const sumRestore=await src2.evaluate(()=>{
    const stake=document.querySelector('#S1 .stake');
    const marks=stake?[...stake.querySelectorAll('.hl')].map(x=>x.textContent):[];
    return {stakeText:stake?(stake.textContent||'').replace(/\s+/g,' ').slice(0,80):null,
      marks, hit:marks.join(' ').includes('Silver vessels travelled')};
  }).catch(e=>({err:String(e).slice(0,120)}));
  out.reopen.summaryRestore=sumRestore;
  const sumAnn=ov2.annotations?.find(a=>a.engine_anchor&&a.engine_anchor.space==='summary')||null;
  out.reopen.summaryStored=sumAnn?{block_id:sumAnn.block_id,space:sumAnn.engine_anchor.space,
    digest:sumAnn.engine_anchor.digest,anchorOn:sumAnn.engine_anchor.anchor.on,
    anchorSection:sumAnn.engine_anchor.anchor.section}:null;
  // title 恢复：annotation 存续（title 无 token 流 → 内核不渲染 marks，只存续不误标）
  const titleAnn=ov2.annotations?.find(a=>a.engine_anchor&&a.engine_anchor.space==='title')||null;
  out.reopen.titleStored=titleAnn?{block_id:titleAnn.block_id,space:titleAnn.engine_anchor.space,
    anchorOn:titleAnn.engine_anchor.anchor.on,note:titleAnn.note.slice(0,20)}:null;
  // 同 id 逐字段对应：note.anchor（内核回执）↔ overlay.engine_anchor.anchor（宿主信封）
  const ovEnvelope=ov2.annotations?.find(a=>a.id===out.hops.noteId)?.engine_anchor||null;
  const noteNow=(refetch.notes||[]).find(n=>n.id===out.hops.noteId)||null;
  if(noteNow&&ovEnvelope&&ovEnvelope.anchor){
    const nA=noteNow.anchor||{},eA=ovEnvelope.anchor;
    out.reopen.fieldCompare={
      on:[nA.on,eA.on],unit:[nA.unit,eA.unit],section:[nA.section,eA.section],
      lang:[nA.lang,eA.lang],level:[nA.level,eA.level],tokens:[nA.tokens,eA.tokens],
      sents:[nA.sents,eA.sents],
      shown:[nA.shown&&nA.shown.words,eA.shown&&eA.shown.words],
      quote:[nA.quote,(eA.quote&&eA.quote.exact||'').slice(0,40)],
      match:{on:nA.on===eA.on,unit:nA.unit===eA.unit,section:nA.section===eA.section,
        lang:nA.lang===eA.lang,level:nA.level===eA.level,
        tokens:JSON.stringify(nA.tokens)===JSON.stringify(eA.tokens),
        sents:JSON.stringify(nA.sents)===JSON.stringify(eA.sents),
        shown:JSON.stringify(nA.shown&&nA.shown.words)===JSON.stringify(eA.shown&&eA.shown.words)},
      noteText:noteNow.text,
    };
  } else {
    out.reopen.fieldCompare={note:!!noteNow,envelope:!!ovEnvelope};
  }
  const rpcAfter=await src2.evaluate(()=>window.__rpcLog||[]).catch(()=>[]);
  out.reopen.rpcNotesGetReply=(rpcAfter||[]).filter(r=>r.method==='GET'&&String(r.path).includes('/api/notes'))
    .map(r=>({path:r.path,replyHead:JSON.stringify(r.reply).slice(0,600)}));
 }catch(e){out.error=out.error||String(e).slice(0,300);}
 await page.screenshot({path:'tmp/reader-anchor-reopen-min.png'}).catch(()=>{});
 await browser.close();
 require('fs').writeFileSync(RESULT,JSON.stringify(out,null,1));
 console.log('REOPEN_MARKS='+JSON.stringify(out.reopen?.after?.marks||null)
   +' STORED='+out.reopen?.annotationStillStored+' DIGEST='+out.reopen?.storedAnchor);
 process.exit(out.reopen?.after?.hit?0:1);
})().catch(e=>{console.error('FATAL',String(e).slice(0,300));process.exit(99);});
