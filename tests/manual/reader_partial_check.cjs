const {chromium}=require(process.env.PW_PACKAGE_PATH);
// 局部失败 / 可阅读分离 真实 UI 验收（生产 dist + 真实 API + 受控 loopback 折叠服务）。
// argv: [web, api, svc, pidsJSON, resultPath, token]
// 原则：
// - 所有交互走真实 UI 点击（#btn-aifold / #btn-foldstatus / #lvlbar / #selPop / 窄屏 tab）；
// - 触发补齐/继续后先捕获 POST 响应，再等 run 真正离开旧终态（run_id/updated_at 变化或
//   status 变 running/pausing），最后等终态——不延时掩盖旧终态竞争；
// - 受控故障生命周期：重试前必须 svcSet 解除注入；每个造错场景在失败后用 /__calls 证明
//   注入生效（否则快速失败，不级联等不存在的按钮）；调用计数用前后差值；
// - 语言事实：译文 lane 与 source lane 各自独立断言（fixture 为真实中文译文 + 中文标题）；
// - 截图前等待可见 lane 文本出现；窄屏 resize 宿主 DOM 身份探针不用等待掩盖。
const WEB=`http://127.0.0.1:${process.argv[2]}`;
const API=`http://127.0.0.1:${process.argv[3]}`;
const SVC=`http://127.0.0.1:${process.argv[4]}`;
const PIDS=JSON.parse(process.argv[5]);   // {src, derive, summary, allfail, stale, pause}
const RESULT=process.argv[6];
const TOKEN=process.argv[7]||'';
const R=[];let CURRENT='?';
const ok=(name,pass,detail)=>{R.push({scenario:CURRENT,name:String(name),pass:!!pass,
  detail:detail===undefined?'':(typeof detail==='object'?JSON.stringify(detail).slice(0,400):String(detail).slice(0,400))});return !!pass;};

if(typeof fetch!=='function'){console.error('需要 Node>=18（全局 fetch）');process.exit(2);}
const api=async(method,path,body)=>{const r=await fetch(API+path,{method,
  headers:{'x-literature-token':TOKEN,'content-type':'application/json'},
  body:body===undefined?undefined:JSON.stringify(body)});
  const t=await r.text();try{return JSON.parse(t);}catch(e){return {status:r.status,text:t.slice(0,200)};}};
const svcGet=async()=>(await fetch(SVC+'/__calls')).json();
const lastMarker=(markers,section)=>{const arr=(markers||[]).filter(m=>m.section===section);
  return arr.length?arr[arr.length-1].marker:null;};
const svcSet=async(cfg)=>(await fetch(SVC+'/__control',{method:'POST',
  headers:{'content-type':'application/json'},body:JSON.stringify(cfg)})).json();
const callCount=(calls,kind,section,lane)=>calls.filter(c=>c.kind===kind&&c.section===section
  &&(lane===undefined||c.target===lane)).length;
const sleep=(ms)=>new Promise(r=>setTimeout(r,ms));
const SCREENSHOT_DIR=require('path').dirname(RESULT);
const SHOTS=[];
const shot=async(name)=>{const p=require('path').join(SCREENSHOT_DIR,name);
  await page.screenshot({path:p,fullPage:false});SHOTS.push(p);return p;};
// overlay PUT 捕获：engine_anchor 断言数据源
const timeline=[];
const attachOverlayTap=()=>{page.on('request',r=>{const u=r.url();
  if(r.method()==='PUT'&&u.includes('/reader/overlay')){
    timeline.push({t:Date.now(),dir:'req',path:u.slice(API.length).split('?')[0]});
    try{const b=r.postDataJSON();
      timeline.push({t:Date.now(),dir:'put-body',
        annotations:(b.overlay?.annotations||[]).map(a=>({id:a.id,pane:a.pane,
          block_id:a.block_id,quote:(a.quote&&a.quote.exact||'').slice(0,40),
          engine_anchor:a.engine_anchor?{space:a.engine_anchor.space,
            locale:a.engine_anchor.locale,digest:a.engine_anchor.digest,
            unit:a.engine_anchor.anchor&&a.engine_anchor.anchor.unit||null}:null}))});}catch(e){}}});
  page.on('response',r=>{const u=r.url();
    if(r.request().method()==='PUT'&&u.includes('/reader/overlay'))
      timeline.push({t:Date.now(),dir:'resp',status:r.status()});});};
const overlayPuts=(sinceTs)=>timeline.filter(e=>e.dir==='req'&&e.t>=sinceTs).length;
const lastPutBody=(sinceTs)=>{const arr=timeline.filter(e=>e.dir==='put-body'&&e.t>=sinceTs);
  return arr.length?arr[arr.length-1]:null;};

let browser,page;
const openReader=async(pid)=>{await page.goto(`${WEB}/#/p/${pid}/reader`,{waitUntil:'domcontentloaded'});
  await page.waitForSelector('#btn-aifold',{timeout:30000});await sleep(800);};
const btnStage=async()=>page.locator('#btn-aifold').getAttribute('data-stage');
const btnLabel=async()=>page.locator('#btn-aifold .rdrr-aifold-label').innerText().catch(()=>'');
const startFold=async()=>{const stage=await btnStage();
  if(stage==='off')await page.locator('#btn-aifold').click();return stage;};
const enterFold=async()=>{await page.locator('#btn-aifold').click();
  await page.waitForSelector('#lvlbar',{timeout:30000});await sleep(1000);};
const statusOpen=async()=>{if(!(await page.locator('.rdrr-foldmenu').count())){
  await page.locator('#btn-foldstatus').click();
  await page.waitForSelector('.rdrr-foldmenu',{timeout:5000});}await sleep(200);};
async function frameOf(pane,timeout=30000){const dl=Date.now()+timeout;
  while(Date.now()<dl){const h=await page.$(`#rdrr-pane-${pane} .rdrr-tfhost iframe`);
    if(h){const f=await h.contentFrame();
      if(f&&await f.evaluate(()=>!!window.__reader).catch(()=>false))return f;}
    await sleep(200);}return null;}
const frameText=async(f)=>f.evaluate(()=>document.getElementById('doc').innerText);
const runSnap=async(pid)=>api('GET',`/projects/${pid}/reader/fold-run`);
const waitRun=async(pid,statuses,timeoutMs=90000)=>{const dl=Date.now()+timeoutMs;let s={};
  while(Date.now()<dl){s=await runSnap(pid);
    if(statuses.includes(s.status))return s;await sleep(200);}return s;};
// 捕获 POST 响应（真实发出的证据），再等 run 离开旧终态，最后等终态。
const capturePost=(urlPart)=>new Promise(res=>{const h=r=>{
    if(r.method()==='POST'&&r.url().includes(urlPart)){
      page.off('request',h);
      r.response().then(rsp=>res({status:rsp.status(),url:r.url().slice(API.length)}))
        .catch(()=>res({status:0,url:urlPart}));}};
  page.on('request',h);setTimeout(()=>{page.off('request',h);res(null);},10000);});
const waitRunRestarted=async(pid,beforeKey,timeoutMs=25000)=>{const dl=Date.now()+timeoutMs;
  while(Date.now()<dl){const s=await runSnap(pid);
    if(s.status==='running'||s.status==='pausing'
      ||JSON.stringify({r:s.run_id,u:s.updated_at,s:s.status})!==beforeKey)return true;
    await sleep(200);}return false;};
const triggerRetry=async(pid)=>{
  const before=await runSnap(pid);
  const beforeKey=JSON.stringify({r:before.run_id,u:before.updated_at,s:before.status});
  const postP=capturePost('/fold-runs');
  await statusOpen();
  await page.locator('.rdrr-foldmenu-ops button',{hasText:'补齐重试'}).first().click();
  const post=await postP;
  ok('点击补齐重试真实发出 POST /fold-runs',!!post&&post.status===200,post);
  const restarted=await waitRunRestarted(pid,beforeKey);
  ok('补齐重试后 run 真正重启（非旧终态回读）',restarted,{});
  return waitRun(pid,['ready','failed']);
};
const triggerResume=async(pid)=>{
  const before=await runSnap(pid);
  const beforeKey=JSON.stringify({r:before.run_id,u:before.updated_at,s:before.status});
  const postP=capturePost('/resume');
  await statusOpen();
  await page.locator('.rdrr-foldmenu-ops button',{hasText:'继续'}).first().click();
  const post=await postP;
  ok('点击继续真实发出 POST /resume',!!post&&post.status===200,post);
  const restarted=await waitRunRestarted(pid,beforeKey);
  ok('继续后 run 真正重启（非旧终态回读）',restarted,{});
  return waitRun(pid,['ready','failed']);
};
// 注入生效证明：失败 run 里必须有被注入 (kind,section) 的调用记录，否则快速失败不级联。
const assertInjected=async(kind,section,label)=>{
  const calls=(await svcGet()).calls;
  const hit=callCount(calls,kind,section);
  ok(`${label}：受控注入生效（/__calls 含 ${kind}:${section}）`,hit>=1,{hit});
  return hit;
};
// fold 内真实拖选（iframe Range 端点 → 父页面 mouse 拖拽 → 宿主 #selPop）
const dragSelectFold=async(frame,paneSel,text)=>{
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
    // 首末字符各自的 clientRect 中点（跨行文本不取整体 bbox 边缘，避免斜跨选错行）
    const rs=document.createRange();rs.setStart(sN,sO);rs.setEnd(sN,Math.min(sN.length,sO+1));
    const r1=rs.getClientRects()[0];
    const re=document.createRange();re.setStart(eN,Math.max(0,eO-1));re.setEnd(eN,eO);
    const r2=re.getClientRects()[re.getClientRects().length-1];
    if(!r1||!r2)return null;
    return {sx:r1.x+r1.width/2,sy:r1.y+r1.height/2,ex:r2.x+r2.width/2,ey:r2.y+r2.height/2};
  },text);
  if(!endpoints)return {dragged:false,reason:'no endpoints',needle:text};
  const box=await page.evaluate(sel=>{const el=document.querySelector(sel+' .rdrr-tfhost iframe');
    const r=el.getBoundingClientRect();return {x:r.x,y:r.y};},paneSel);
  const x1=box.x+endpoints.sx-2,y1=box.y+endpoints.sy;
  const x2=box.x+endpoints.ex+2,y2=box.y+endpoints.ey;
  await page.mouse.move(x1,y1);await page.mouse.down();
  for(let k=1;k<=10;k++)await page.mouse.move(x1+(x2-x1)*k/10,y1+(y2-y1)*k/10);
  await page.mouse.up();await sleep(1200);
  const selStr=await frame.evaluate(()=>{const sel=window.getSelection();
    return sel&&!sel.isCollapsed?String(sel).slice(0,40):'';}).catch(()=> '');
  let hostSelPop=await page.evaluate(()=>{const p=document.querySelector('#selPop');
    return p?{visible:p.getBoundingClientRect().width>0,
      acts:[...p.querySelectorAll('button')].map(b=>(b.textContent||'').trim())}:null;});
  for(let i=0;i<2&&!hostSelPop;i++){
    await frame.evaluate(()=>{const doc=document.getElementById('doc');
      const sel=window.getSelection();
      if(sel&&!sel.isCollapsed){const r=sel.getRangeAt(0).getBoundingClientRect();
        doc.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,
          clientX:r.x+r.width/2,clientY:r.y+r.height/2}));}});
    await sleep(900);
    hostSelPop=await page.evaluate(()=>{const p=document.querySelector('#selPop');
      return p?{visible:p.getBoundingClientRect().width>0,
        acts:[...p.querySelectorAll('button')].map(b=>(b.textContent||'').trim())}:null;});
  }
  return {dragged:true,hostSelPop,selStr};
};
// 当前可见 lane 文本（窄屏单栏：只认可见容器，不选 hidden iframe）
const visibleLane=async()=>page.evaluate(()=>{
  for(const pane of ['source','trans','origin']){
    const sec=document.getElementById('rdrr-pane-'+pane);
    if(!sec)continue;
    const rect=sec.getBoundingClientRect();
    if(rect.width<=0||rect.height<=0)continue;
    const f=sec.querySelector('.rdrr-tfhost iframe');
    if(f&&f.contentDocument){const d=f.contentDocument.getElementById('doc');
      if(d&&d.innerText.trim().length>20)return {pane,text:d.innerText.slice(0,100)};}
    const a=sec.querySelector('.rdrr-article');
    if(a&&a.innerText.trim().length>20)return {pane,text:a.innerText.slice(0,100)};
  }
  return null;});
// 宿主/iframe 探针：resize 前后 DOM 身份、iframe 数量、ready、文本长度（不用等待掩盖）
const probeHosts=async(tag)=>page.evaluate((tag)=>{
  const out={tag};
  for(const pane of ['source','trans']){
    const host=document.querySelector(`#rdrr-pane-${pane} .rdrr-tfhost`);
    const iframes=document.querySelectorAll(`#rdrr-pane-${pane} .rdrr-tfhost iframe`);
    let ready=false,textLen=0;
    if(iframes[0]){
      try{ready=!!iframes[0].contentWindow.__reader;
        const d=iframes[0].contentDocument&&iframes[0].contentDocument.getElementById('doc');
        textLen=d?d.innerText.trim().length:0;}catch(e){}}
    let probeId=null;
    if(host){if(!host.dataset.probeid)host.dataset.probeid='host-'+pane;
      probeId=host.dataset.probeid;}
    out[pane]={hostPresent:!!host,probeId,iframeCount:iframes.length,ready,textLen};
  }
  return out;
},tag);

// fixture 语言事实（runner 提供真实中文译文 + 中文标题；classify 双语识别）
const S1_EN='Silver vessels travelled along the Silk Road';
const S2_EN='Hoards cluster in port cities';
const S1_ZH='白银器皿沿丝绸之路流转';
const S2_ZH='窖藏集中在港口城市';
const NEW_EN='New evidence emerged later on';

// 场景 A：少量源失败 → readable 进入折叠 + 5 层 pending 全文 + 语言 + 补齐重试
async function scenarioSrc(){
  const pid=PIDS.src;
  await svcSet({fail:{source:['S2']},omit:[],delay_ms:0});
  await openReader(pid);
  const srcTxt=await page.locator('#rdrr-pane-source p[data-block-id="b-p-1"]').innerText().catch(()=>'');
  const transTxt=await page.locator('#rdrr-pane-trans p[data-block-id="b-p-1"]').innerText().catch(()=>'');
  ok('原文栏为英文原文',srcTxt.includes(S1_EN)&&!srcTxt.includes(S1_ZH),srcTxt.slice(0,60));
  ok('译文栏为中文译文',transTxt.includes(S1_ZH)&&!transTxt.includes(S1_EN),transTxt.slice(0,60));
  const stage=await startFold();ok('点击「生成折叠」启动整篇处理',stage==='off',{stage});
  const failed=await waitRun(pid,['failed','ready']);ok('S2 源失败 → run failed',failed.status==='failed',
    {status:failed.status,errors:(failed.errors||[]).map(e=>String(e.error||'').slice(0,120))});
  await assertInjected('source','S2','S2 源失败');
  ok('partial run readable=true（有已验证成果）',failed.readable===true,{readable:failed.readable});
  ok('ready 严格不放松',failed.ready===false,{ready:failed.ready});
  const avail=failed.availability||{};
  ok('availability 槽位恒等式',avail.units_total===(avail.units_valid||0)+(avail.units_pending||0)+(avail.units_suppressed||0),avail);
  ok('availability.source_units_pending>=1',(avail.source_units_pending||0)>=1,avail.source_units_pending);
  await sleep(1500);
  ok('失败后按钮变为「进入折叠」',(await btnLabel())==='进入折叠',await btnLabel());
  await enterFold();ok('真实点击进入折叠视图（#lvlbar）',true);
  const f=await frameOf('trans'),fs=await frameOf('source');
  ok('折叠译文 iframe 载入',!!f);ok('折叠原文 iframe 载入',!!fs);
  if(f&&fs){
    for(const L of [1,2,3,4,5]){
      await page.locator(`#lvlbar [data-l="${L}"]`).click();await sleep(700);
      const txt=await frameText(f);
      ok(`目标栏 L${L}：pending 节显示中文全文`,txt.includes(S2_ZH)&&!txt.includes(S2_EN),{L});
      const pend=await f.evaluate(()=>document.querySelectorAll('.is-pend').length);
      ok(`目标栏 L${L}：未生成徽标可见`,pend>=1,{L,pend});
      if(pend){const badge=await f.evaluate(()=>{const el=document.querySelector('.u.is-pend');
        return {content:getComputedStyle(el,'::before').content};});
        ok(`目标栏 L${L}：未生成徽标真实渲染（::before 含未生成）`,String(badge.content).includes('未生成'),badge);}
      const stxt=await frameText(fs);
      ok(`原文栏 L${L}：英文全文可见`,stxt.includes(S2_EN)&&!stxt.includes(S2_ZH),{L});
    }
    // 留档截图：主线层（pending 全文）+ 状态菜单；主题层
    await page.locator('#lvlbar [data-l="4"]').click();await sleep(800);
    await statusOpen();await sleep(300);
    await shot('reader-partial-desktop-menu.png');
    await page.keyboard.press('Escape').catch(()=>{});await sleep(300);
    await page.locator('#lvlbar [data-l="5"]').click();await sleep(800);
    await shot('reader-partial-desktop-theme.png');
    await page.locator('#lvlbar [data-l="3"]').click();await sleep(600);
    // pending 短层划选保存回归：Full 词空间 engine_anchor + 刷新读回
    // 目标 unit 先 scrollIntoView（虚拟渲染需滚动后才实例化字形），再取端点
    // 直接定位 pending unit（.u.is-pend）滚动到视口中心，避免文本节点 closest 误命中摘要
    const scrolled=await f.evaluate(()=>{
      const el=document.querySelector('.u.is-pend');
      if(!el)return {ok:false,reason:'no .is-pend'};
      el.scrollIntoView({block:'center'});
      return {ok:true,unit:el.getAttribute('data-u')||''};
    });
    await sleep(800);
    ok('pending unit scrollIntoView 完成',scrolled.ok,scrolled);
    const drag=await dragSelectFold(f,'#rdrr-pane-trans',S2_ZH);
    ok('pending unit 上真实拖选 → 宿主 #selPop',drag.dragged&&drag.hostSelPop&&drag.hostSelPop.visible,
      {hostSelPop:drag.hostSelPop,selStr:drag.selStr});
    if(drag.hostSelPop&&drag.hostSelPop.visible){
      const putStart=Date.now();
      await page.locator('#selPop .rdrr-swatch').first().click();
      for(let i=0;i<20&&overlayPuts(putStart)===0;i++)await sleep(200);
      await sleep(800);
      const body=lastPutBody(putStart);
      const ann=body&&body.annotations&&body.annotations.find(a=>a.engine_anchor);
      const ea=ann&&ann.engine_anchor;
      ok('pending 划选保存 → PUT overlay 携带 engine_anchor',!!ea,body&&{n:body.annotations.length});
      ok('engine_anchor.space === "full"（Full 词空间）',!!ea&&ea.space==='full',ea);
      ok('engine_anchor.digest 为 Full token 指纹',!!ea&&/^[0-9a-f]{8}$/.test(String(ea.digest||'')),ea&&ea.digest);
      ok('engine_anchor.locale 为目标语言 zh-CN',!!ea&&ea.locale==='zh-CN',ea&&ea.locale);
      const hl=await f.evaluate(()=>document.querySelectorAll('#doc .hl, #doc span.tk.hl').length);
      ok('保存后折叠 iframe 内高亮可见',hl>=1,{hl});
      await page.reload({waitUntil:'domcontentloaded'});
      await page.waitForSelector('#btn-aifold',{timeout:30000});await sleep(1500);
      let state=null,saved=null;
      for(let i=0;i<5&&!saved;i++){
        state=await api('GET',`/projects/${pid}/reader/state`).catch(()=>null);
        const list=(state&&state.overlay&&state.overlay.annotations)||[];
        saved=list.find(a=>a.engine_anchor&&a.engine_anchor.space==='full')||null;
        if(!saved)await sleep(500);
      }
      ok('刷新后 annotation 从盘读回（space=full）',!!saved,
        {n:state&&state.overlay?((state.overlay.annotations||[]).length):0});
      ok('读回 digest 与保存一致',!!saved&&!!ea&&saved.engine_anchor.digest===ea.digest,
        {saved:saved&&saved.engine_anchor&&saved.engine_anchor.digest});
      const st2=await btnStage();
      if(st2!=='on'){await enterFold();}
      const f2=await frameOf('trans');
      if(f2){await sleep(600);
        const hl2=await f2.evaluate(()=>document.querySelectorAll('#doc .hl, #doc span.tk.hl').length);
        ok('刷新后原生锚高亮重建（非仅 textContent）',hl2>=1,{hl2});}
    }
    // 记录 iframe DOM 身份（补齐运行期间/完成后未手动载入前身份应不变）
    const probe=await page.evaluate(()=>{
      const el=document.querySelector('#rdrr-pane-trans .rdrr-tfhost iframe');
      if(!el)return null;el.dataset.probeid='probe-src-'+Date.now();
      return {id:el.dataset.probeid};});
    // 受控故障生命周期：先解除注入，再补齐（提示由 pending-refresh 保证，不用 delay 掩盖）
    await svcSet({fail:{source:[]},omit:[],delay_ms:0});
    let calls=(await svcGet()).calls;
    const s1src=callCount(calls,'source','S1'),s3src=callCount(calls,'source','S3');
    const s2src0=callCount(calls,'source','S2'),s2drv0=callCount(calls,'derive','S2');
    const done=await triggerRetry(pid);
    ok('补齐重试后 ready',done.status==='ready',
      {status:done.status,errors:(done.errors||[]).map(e=>String(e.error||'').slice(0,120))});
    const preLoad=await page.evaluate(()=>{
      const el=document.querySelector('#rdrr-pane-trans .rdrr-tfhost iframe');
      return {probe:el&&el.dataset.probeid,connected:!!el};});
    ok('补齐完成、未手动载入前 iframe 身份不变',!!preLoad.probe&&preLoad.probe===probe.id,
      {before:probe.id,after:preLoad.probe});
    const fOld=await frameOf('trans');
    if(fOld){const pendOld=await fOld.evaluate(()=>document.querySelectorAll('.is-pend').length);
      ok('未手动载入前旧成果仍显示（S2 仍 pending）',pendOld>=1,{pendOld});}
    calls=(await svcGet()).calls;
    ok('成功节 S1 源折叠 0 新调用',callCount(calls,'source','S1')===s1src,{s1src,now:callCount(calls,'source','S1')});
    ok('成功节 S3 源折叠 0 新调用',callCount(calls,'source','S3')===s3src,{s3src,now:callCount(calls,'source','S3')});
    ok('失败节 S2 恰好各补一次（源/派生差值 +1）',
      callCount(calls,'source','S2')===s2src0+1&&callCount(calls,'derive','S2')===s2drv0+1,
      {src:[s2src0,callCount(calls,'source','S2')],drv:[s2drv0,callCount(calls,'derive','S2')]});
    await svcSet({fail:{source:[]},omit:[],delay_ms:0});
    // 受控故障生命周期：先解除注入，再补齐（退出重进生命周期块移到载入断言之后：
    // exitFold 会清 newerResults，先退出会让载入提示断言失真）
    await statusOpen();
    const newBtn=page.locator('.rdrr-foldmenu-ops button',{hasText:'载入新成果'});
    // API ready ≠ React 同步完成：等按钮可见（最长 10s），失败时记录 menu 文字与 run 快照
    const newBtnVisible=await newBtn.first().waitFor({state:'visible',timeout:10000})
      .then(()=>true).catch(()=>false);
    const diagReady=await runSnap(pid);
    const diagMenu=await page.evaluate(()=>{const m=document.querySelector('.rdrr-foldmenu');
      return m?m.innerText.slice(0,160):null;});
    ok('完成后提示载入新成果（等待可见而非即时 count）',newBtnVisible,
      {menu:diagMenu,snap:{status:diagReady.status,updated_at:diagReady.updated_at,
        errors:(diagReady.errors||[]).map(e=>String(e.error||'').slice(0,80))}});
    if(newBtnVisible){
      const fCur=await frameOf('trans');
      const visBefore=await fCur.evaluate(()=>{const vh=window.innerHeight;
        const els=[...document.querySelectorAll('#doc .u[data-u]')];
        for(const el of els){const r=el.getBoundingClientRect();
          if(r.bottom>60&&r.top<vh&&r.height>0)return el.getAttribute('data-u');}
        return null;});
      await newBtn.first().click();await sleep(2500);
      const afterState=await page.evaluate(()=>{
        const el=document.querySelector('#rdrr-pane-trans .rdrr-tfhost iframe');
        return {probe:el&&el.dataset.probeid};});
      const oldProbeGone=await page.evaluate(
        id=>!document.querySelector(`[data-probeid="${id}"]`),probe.id);
      ok('主动载入后 iframe 按新 data 重建（旧 probe 元素消失）',oldProbeGone,
        {before:probe.id,after:afterState.probe});
      const selPopGone=await page.evaluate(()=>{const p=document.querySelector('#selPop');
        return !p||p.getBoundingClientRect().width===0;});
      ok('载入后旧选区 popup 已清理',selPopGone,{});
      const f3=await frameOf('trans');
      if(f3){await sleep(600);
        const pend=await f3.evaluate(()=>document.querySelectorAll('.is-pend').length);
        const txt=await frameText(f3);
        ok('载入后真实换新数据（S2 不再 pending）',pend===0&&txt.includes(S2_ZH),{pend});
        if(visBefore){const vh=page.viewportSize().height;
          const vis=await f3.evaluate((uid)=>{const el=document.querySelector(`#doc .u[data-u="${uid}"]`);
            if(!el)return {found:false,top:null};const r=el.getBoundingClientRect();
            return {found:r.bottom>0&&r.top<window.innerHeight,top:Math.round(r.top)};},visBefore);
          ok('载入后恢复原可见 unit（uid 在视口；短 fixture 未验 ratio，如实边界）',
            vis.found===true,vis);}}
    }
    // 退出折叠→重进：正文存在（宿主生命周期回归；必须在载入断言之后，exitFold 清 newerResults）
    await page.locator('#btn-aifold').click();await sleep(800);
    const exitLabel=await btnLabel();
    ok('退出折叠后回到普通阅读（按钮文案）',exitLabel==='进入折叠'||exitLabel==='生成折叠',exitLabel);
    await page.locator('#btn-aifold').click();
    await page.waitForSelector('#lvlbar',{timeout:30000});await sleep(1000);
    const fRe=await frameOf('trans');
    if(fRe){const txtRe=await frameText(fRe);
      ok('重进折叠后正文存在',txtRe.includes(S2_ZH)||txtRe.includes('译要点')||txtRe.includes(S1_ZH),
        {len:txtRe.length});}
  }
}

// 场景 B：仅目标派生失败 → 中文全文不回退英文、源/派生独立
async function scenarioDerive(){
  const pid=PIDS.derive;
  await svcSet({fail:{derive:['S2']},omit:[],delay_ms:0});
  await openReader(pid);
  await startFold();
  const failed=await waitRun(pid,['failed','ready']);ok('派生失败 → run failed',failed.status==='failed',
    {status:failed.status,errors:(failed.errors||[]).map(e=>String(e.error||'').slice(0,120))});
  await assertInjected('derive','S2','S2 派生失败');
  const avail=failed.availability||{};
  ok('source lane 全有效（source_units_pending=0）',(avail.source_units_pending||0)===0,avail.source_units_pending);
  ok('target lane 有待补（target_units_pending>=1）',(avail.target_units_pending||0)>=1,avail.target_units_pending);
  ok('派生失败仍 readable=true',failed.readable===true,{readable:failed.readable});
  await sleep(1500);await enterFold();
  const f=await frameOf('trans'),fs=await frameOf('source');
  if(f&&fs){
    await page.locator('#lvlbar [data-l="3"]').click();await sleep(700);
    const txt=await frameText(f);
    ok('派生失败节显示中文全文（自身 translation 事实）',txt.includes(S2_ZH),'');
    ok('派生失败不回退英文全文',!txt.includes(S2_EN),'');
    const pend=await f.evaluate(()=>document.querySelectorAll('.is-pend').length);
    ok('目标栏存在未生成徽标',pend>=1,{pend});
    const srcPend=await fs.evaluate(()=>document.querySelectorAll('.is-pend').length);
    ok('原文栏无未生成徽标（源/派生独立降级）',srcPend===0,{srcPend});
  }
  await svcSet({fail:{derive:[]},omit:[],delay_ms:0});
  const calls=(await svcGet()).calls;
  const s2src=callCount(calls,'source','S2'),s2drv=callCount(calls,'derive','S2');
  const done=await triggerRetry(pid);
  ok('派生失败补齐后 ready',done.status==='ready',
    {status:done.status,errors:(done.errors||[]).map(e=>String(e.error||'').slice(0,120))});
  const now=(await svcGet()).calls;
  ok('源层 S2 不重折（0 新源调用）',callCount(now,'source','S2')===s2src,{s2src,now:callCount(now,'source','S2')});
  ok('派生 S2 恰好补一次（差值 +1）',callCount(now,'derive','S2')===s2drv+1,{s2drv,now:callCount(now,'derive','S2')});
}

// 场景 C：仅章节摘要失败 → unit 短层保留、resume 只补摘要
async function scenarioSummary(){
  const pid=PIDS.summary;
  await svcSet({fail:{summary:['S1']},omit:['S1'],delay_ms:0});
  await openReader(pid);
  await startFold();
  const failed=await waitRun(pid,['failed','ready']);ok('摘要失败 → run failed',failed.status==='failed',
    {status:failed.status,errors:(failed.errors||[]).map(e=>String(e.error||'').slice(0,120))});
  await assertInjected('summary','S1','S1 摘要失败');
  const avail=failed.availability||{};
  ok('摘要待补计入 availability（summaries_pending>=1）',(avail.summaries_pending||0)>=1,avail.summaries_pending);
  ok('unit 短层有效（units_valid>=1）',(avail.units_valid||0)>=1,avail.units_valid);
  await sleep(1500);await enterFold();
  const f=await frameOf('trans');
  if(f){
    await page.locator('#lvlbar [data-l="3"]').click();await sleep(700);
    const txt=await frameText(f);
    ok('S1 unit 派生短层保留（译要点可见）',txt.includes('译要点'),'');
    ok('S1 章节摘要不显示（lit_summary_pending → 不给假摘要）',!txt.includes('#DS1'),'');
    ok('其他节章节摘要正常（生成标记在）',txt.includes('#DS2')||txt.includes('#DS3'),'');
    const spend=await f.evaluate(()=>{const c=document.querySelector('.chunk.is-spend');
      if(!c)return null;const hd=c.querySelector('.hd');
      return {found:true,after:hd?getComputedStyle(hd,'::after').content:null};});
    ok('摘要待补徽标真实渲染（.is-spend ::after 含摘要未生成）',
      !!spend&&String(spend.after).includes('摘要未生成'),spend);
  }
  await svcSet({fail:{summary:[]},omit:[],delay_ms:0});
  let calls=(await svcGet()).calls;
  const s1src=callCount(calls,'source','S1'),s1drv=callCount(calls,'derive','S1');
  const callCountPre={sumSrc:callCount(calls,'summary','S1',false),sumTgt:callCount(calls,'summary','S1',true)};
  const done=await triggerRetry(pid);
  ok('摘要失败补齐后 ready',done.status==='ready',
    {status:done.status,errors:(done.errors||[]).map(e=>String(e.error||'').slice(0,120))});
  calls=(await svcGet()).calls;
  ok('resume 只补摘要：S1 无新 source/derive 调用',
    callCount(calls,'source','S1')===s1src&&callCount(calls,'derive','S1')===s1drv,
    {src:[s1src,callCount(calls,'source','S1')],drv:[s1drv,callCount(calls,'derive','S1')]});
  ok('S1 摘要恰好补发：源/目标 lane 各 +1',
    callCount(calls,'summary','S1',false)===callCountPre.sumSrc+1
    &&callCount(calls,'summary','S1',true)===callCountPre.sumTgt+1,
    {src:[callCountPre.sumSrc,callCount(calls,'summary','S1',false)],
     tgt:[callCountPre.sumTgt,callCount(calls,'summary','S1',true)]});
}

// 场景 D：全部失败 → readable=false、普通全文可读、不假称 AI 就绪、不得进入折叠
async function scenarioAllFail(){
  const pid=PIDS.allfail;
  await svcSet({fail:{source:['S1','S2','S3']},omit:[],delay_ms:0});
  await openReader(pid);
  await startFold();
  const failed=await waitRun(pid,['failed','ready']);ok('全部失败 → run failed',failed.status==='failed',
    {status:failed.status,errors:(failed.errors||[]).map(e=>String(e.error||'').slice(0,120))});
  await assertInjected('source','S1','S1 注入');
  await assertInjected('source','S2','S2 注入');
  await assertInjected('source','S3','S3 注入');
  const avail=failed.availability||{};
  ok('全部失败 readable=false',failed.readable===false,{readable:failed.readable});
  ok('units_valid=0（不假称有任何 AI 成果）',(avail.units_valid||0)===0,avail.units_valid);
  ok('full_text=true（普通全文仍可读）',avail.full_text===true,avail.full_text);
  await sleep(1500);
  const disabled=await page.locator('#btn-aifold').isDisabled().catch(()=>false);
  ok('无有效 AI 时不得进入折叠（按钮禁用）',disabled,{});
  ok('无 #lvlbar（未进入折叠视图）',(await page.locator('#lvlbar').count())===0,'');
  const srcTxt=await page.locator('#rdrr-pane-source p[data-block-id="b-p-1"]').innerText().catch(()=>'');
  ok('普通全文可读（原文栏正常）',srcTxt.includes(S1_EN),srcTxt.slice(0,50));
  await statusOpen();
  const menuTxt=await page.locator('.rdrr-foldmenu').innerText().catch(()=>'');
  ok('状态入口不假称成果就绪',!/成果就绪/.test(menuTxt),menuTxt.slice(0,120));
  ok('状态入口显示待补计数',/待补/.test(menuTxt),menuTxt.slice(0,120));
  await page.keyboard.press('Escape').catch(()=>{});await sleep(300);
  await svcSet({fail:{source:[]},omit:[],delay_ms:0});
  const done=await triggerRetry(pid);
  ok('全部失败后可恢复到 ready',done.status==='ready',
    {status:done.status,errors:(done.errors||[]).map(e=>String(e.error||'').slice(0,120))});
}

// 场景 E：过期数据 → 安全投影（b-p-1 拒绝、b-p-2 未变保留）、resume 精准补
async function scenarioStale(){
  const pid=PIDS.stale;
  await svcSet({fail:{},omit:[],delay_ms:0});
  await openReader(pid);
  const snap=await runSnap(pid);
  ok('事实变化后 ready 诚实失效（failed）',snap.status==='failed',{status:snap.status});
  ok('失效后仍 readable=true（未变节有效）',snap.readable===true,{readable:snap.readable});
  await sleep(1500);await enterFold();
  const f=await frameOf('trans'),fs=await frameOf('source');
  if(f&&fs){
    await page.locator('#lvlbar [data-l="3"]').click();await sleep(700);
    const stxt=await frameText(fs);
    ok('安全投影：source lane 反映当前事实（新句在）',stxt.includes(NEW_EN),'');
    const txt=await frameText(f);
    ok('trans lane 与自身 translation 事实一致（原中文保留）',txt.includes(S1_ZH),'');
    ok('trans lane 不混入 source 新句（lane 独立）',!txt.includes(NEW_EN),'');
    ok('安全投影：S1 旧派生短层不复活（无 #DS1-1）',!txt.includes('#DS1-1'),'');
    ok('未变化节 S3 短层保留（#DS3-1 在）',txt.includes('#DS3-1'),'');
  }
  await svcSet({fail:{},omit:[],delay_ms:0});
  let calls=(await svcGet()).calls;
  const s3src=callCount(calls,'source','S3'),s3drv=callCount(calls,'derive','S3');
  // resume 前基线：native S1 b-p-1 待补（真过期被拒绝、短层空）
  const preNative=await api('GET',`/projects/${pid}/reader/layers/native?locale=zh-CN`).catch(()=>null);
  const preB1=((preNative?.units?.['b-p-1']||{}).tr||{})['zh-CN']||{};
  const preB2=((preNative?.units?.['b-p-2']||{}).tr||{})['zh-CN']||{};
  ok('重试前真正变化的 b-p-1 短层待补',preB1.lit_pending===true,preB1);
  ok('重试前未变化的 b-p-2 短层保留',preB2.lit_pending===false,preB2);
  const done=await triggerRetry(pid);
  ok('过期数据 resume 后 ready',done.status==='ready',
    {status:done.status,errors:(done.errors||[]).map(e=>String(e.error||'').slice(0,120))});
  calls=(await svcGet()).calls;
  ok('未变化节 S3 缓存 0 新调用',
    callCount(calls,'source','S3')===s3src&&callCount(calls,'derive','S3')===s3drv,
    {src:[s3src,callCount(calls,'source','S3')],drv:[s3drv,callCount(calls,'derive','S3')]});
  await svcSet({fail:{},omit:[],delay_ms:0});
  // resume 后事实安全：b-p-1 已补齐（lit_pending=false + 有效短层）；b-p-2 未变保留
  const postNative=await api('GET',`/projects/${pid}/reader/layers/native?locale=zh-CN`).catch(()=>null);
  const pU=(postNative&&postNative.units)||{};
  const postB1=((pU['b-p-1']||{}).tr||{})['zh-CN']||{};
  const postB2=((pU['b-p-2']||{}).tr||{})['zh-CN']||{};
  ok('resume 后 b-p-1 已补齐（lit_pending=false 且短层有效）',
    postB1.lit_pending===false&&Array.isArray(postB1.lad?.brief)&&postB1.lad.brief.length>0,postB1);
  ok('resume 后未变 b-p-2 短层保留（不标 pending）',
    postB2.lit_pending===false&&Array.isArray(postB2.lad?.brief)&&postB2.lad.brief.length>0,postB2);
  ok('未变 b-p-2 有效短层逐字不变',JSON.stringify(preB2.lad?.brief)===JSON.stringify(postB2.lad?.brief),{});
  // 手动载入：文本等于新 native（S1 take 逐字）
  await statusOpen();
  const staleBtn=page.locator('.rdrr-foldmenu-ops button',{hasText:'载入新成果'});
  const staleBtnVisible=await staleBtn.first().waitFor({state:'visible',timeout:10000})
    .then(()=>true).catch(()=>false);
  ok('过期补齐后提示载入新成果（等待可见）',staleBtnVisible,{});
  if(staleBtnVisible){
    const probeBefore=await page.evaluate(()=>{
      const el=document.querySelector('#rdrr-pane-trans .rdrr-tfhost iframe');
      if(!el)return null;el.dataset.probeid='probe-stale-'+Date.now();
      return el.dataset.probeid;});
    await staleBtn.first().click();await sleep(3000);
    const probeAfter=await page.evaluate(()=>{
      const el=document.querySelector('#rdrr-pane-trans .rdrr-tfhost iframe');
      return {probe:el&&el.dataset.probeid,connected:!!el};});
    ok('stale 主动载入后 iframe 重建（connected 且身份更换）',
      probeAfter.connected&&probeAfter.probe!==probeBefore,
      {before:probeBefore,after:probeAfter.probe});
    const nativeStale=await api('GET',`/projects/${pid}/reader/layers/native?locale=zh-CN`).catch(()=>null);
    const staleChunk=nativeStale&&((nativeStale.chunks||[]).find(x=>x.id==='S1')||null);
    const staleTake=((((staleChunk||{}).tr||{})['zh-CN']||{}).lad||{}).take||[];
    const takeJoined=staleTake.map(tk=>(tk.t||'')+(tk.s?' ':'')).join('').trim();
    ok('stale native 记录 S1 当前 take',takeJoined.length>0,{takeJoined:takeJoined.slice(0,80)});
    const fDiag=await frameOf('trans');
    if(fDiag){
      // L5（主题，stake 恒显）读取 DOM 与 native take 逐字对比
      await page.locator('#lvlbar [data-l="5"]').click();await sleep(800);
      const t=await frameText(fDiag);
      ok('手动载入文本等于新 native（S1 take 逐字在 DOM）',
        takeJoined.length>0&&t.replace(/\s+/g,'').includes(takeJoined.replace(/\s+/g,'')),
        {takeJoined:takeJoined.slice(0,100),latestServiceMarker:lastMarker((await svcGet()).markers,'S1')});
      const staleOldGone=await page.evaluate(id=>{
        const el=document.querySelector('#rdrr-pane-trans .rdrr-tfhost iframe');
        return !!el&&el.dataset.probeid!==id;},probeBefore);
      ok('stale 载入后 iframe 身份更换（connected 且非旧 probe）',staleOldGone,{});}
  }
}

// 场景 F：暂停重试（请求边界暂停）+ 刷新恢复 + 项目隔离
async function scenarioPause(){
  const pid=PIDS.pause;
  await svcSet({fail:{},omit:[],delay_ms:600});
  await openReader(pid);
  await startFold();
  await sleep(900);            // 首个请求在途
  await statusOpen();
  await page.locator('.rdrr-foldmenu-ops button',{hasText:'暂停'}).first().click();
  await svcSet({delay_ms:0});
  const paused=await waitRun(pid,['paused','ready','failed'],60000);
  ok('请求边界暂停生效（paused）',paused.status==='paused',paused.status);
  await sleep(1000);
  await page.reload({waitUntil:'domcontentloaded'});
  await page.waitForSelector('#btn-aifold',{timeout:30000});await sleep(1000);
  const pausedAttr=await page.locator('#btn-aifold').getAttribute('data-paused');
  ok('刷新后暂停状态从盘恢复（data-paused=1）',pausedAttr==='1',{pausedAttr});
  const afterReload=await runSnap(pid);
  ok('刷新后快照仍 paused 且 readable（有已落盘成果）',
    afterReload.status==='paused'&&afterReload.readable===true,
    {status:afterReload.status,readable:afterReload.readable});
  let calls=(await svcGet()).calls;const s1=callCount(calls,'source','S1');
  const done=await triggerResume(pid);
  ok('继续后 ready',done.status==='ready',
    {status:done.status,errors:(done.errors||[]).map(e=>String(e.error||'').slice(0,120))});
  calls=(await svcGet()).calls;
  ok('S1 源折叠不重复计费（差值 0）',callCount(calls,'source','S1')===s1,{s1,now:callCount(calls,'source','S1')});
  await openReader(PIDS.src);
  const srcSnap=await runSnap(PIDS.src);
  ok('切项目后原项目 run 状态不变（ready）',srcSnap.status==='ready',{status:srcSnap.status});
  const pauseSnap=await runSnap(pid);
  ok('切项目后暂停项目状态不变（ready）',pauseSnap.status==='ready',{status:pauseSnap.status});
}

// 场景 G：resize 宿主 DOM 身份探针（不等待掩盖）+ 窄屏菜单 bbox/可滚 + 截图
async function scenarioNarrow(){
  const pid=PIDS.src;
  await openReader(pid);
  const st=await btnStage();
  if(st!=='on'){await enterFold();}
  // desktop 基线探针
  const before=await probeHosts('desktop');
  ok('desktop 折叠宿主基线：两栏 tfhost 在位且 iframe ready',
    before.source&&before.source.hostPresent&&before.source.ready
    &&before.trans&&before.trans.ready,before);
  for(const width of [390,320,768]){
    await page.setViewportSize({width,height:820});await sleep(400);   // 仅让 React 提交，不重试掩盖
    const after=await probeHosts('w'+width);
    ok(`resize ${width}px：tfhost DOM 身份未更换（无未 key 重建）`,
      after.source&&before.source&&after.source.probeId===before.source.probeId
      &&after.trans.probeId===before.trans.probeId,after);
    ok(`resize ${width}px：iframe 仍 ready 且文本非空`,
      after.source&&after.source.ready&&after.source.textLen>30
      &&after.trans&&after.trans.ready&&after.trans.textLen>30,after);
    if(width===390||width===320){
      // 点窄屏「原文」tab 显示正文，再等可见 lane 文本（选 visible 容器）
      const tab=page.locator('.rdrr-narrowtabs button',{hasText:'原文'});
      if(await tab.count())await tab.first().click();
      let lane=null;
      for(let i=0;i<12&&!lane;i++){lane=await visibleLane();if(!lane)await sleep(300);}
      ok(`窄屏 ${width}px：可见 lane 文本已出现（截图非空白）`,!!lane,lane);
      const overflow=await page.evaluate(()=>document.documentElement.scrollWidth-window.innerWidth);
      ok(`窄屏 ${width}px 无横向溢出`,overflow<=1,{overflow});
      await statusOpen();await sleep(500);
      const box=await page.locator('.rdrr-foldmenu').boundingBox().catch(()=>null);
      const inViewport=box&&box.x>=-1&&box.y>=-1
        &&box.x+box.width<=width+1&&box.y+box.height<=821;
      ok(`窄屏 ${width}px 菜单 bbox 在视口内`,!!inViewport,box);
      const scr=await page.evaluate(()=>{const m=document.querySelector('.rdrr-foldmenu');
        return m?{sh:m.scrollHeight,ch:m.clientHeight,of:getComputedStyle(m).overflowY}:null;});
      ok(`窄屏 ${width}px 菜单限高可滚（ch<=视口内且 sh>=ch）`,
        !!scr&&scr.ch<=820&&scr.sh>=scr.ch,scr);
      await shot(`reader-partial-narrow-${width}.png`);
      await page.keyboard.press('Escape').catch(()=>{});await sleep(300);
    }
  }
  await page.setViewportSize({width:1440,height:920});await sleep(600);
  const back=await probeHosts('desktop-again');
  ok('390→宽屏：iframe 仍 ready 且正文存在',
    back.source&&back.source.ready&&back.source.textLen>30
    &&back.trans&&back.trans.ready&&back.trans.textLen>30,back);
}

(async()=>{
  try{
    browser=await chromium.launch({headless:true});
    page=await browser.newPage({viewport:{width:1440,height:920}});
    attachOverlayTap();
    page.on('pageerror',e=>ok('页面脚本异常',false,String(e).slice(0,200)));
    const scenarios=[['src',scenarioSrc],['derive',scenarioDerive],['summary',scenarioSummary],
      ['allfail',scenarioAllFail],['stale',scenarioStale],['pause',scenarioPause],['narrow',scenarioNarrow]];
    for(const [name,fn] of scenarios){
      CURRENT=name;
      try{await fn();}
      catch(e){ok('场景异常',false,String(e&&e.stack||e).slice(0,400));}
    }
    CURRENT='summary';
    const failed=R.filter(x=>!x.pass).length;
    const out={results:R,failed,total:R.length,screenshots:SHOTS};
    require('fs').writeFileSync(RESULT,JSON.stringify(out),'utf-8');
    console.log(`TOTAL ${R.length} FAILED ${failed}`);
    for(const x of R)if(!x.pass)console.log(`FAIL [${x.scenario}] ${x.name} :: ${x.detail}`);
    process.exit(failed?1:0);
  }catch(e){
    console.error(e&&e.stack||e);
    try{require('fs').writeFileSync(RESULT,JSON.stringify({results:R,failed:R.filter(x=>!x.pass).length+1,fatal:String(e).slice(0,400)}),'utf-8');}catch(_){}
    process.exit(2);
  }finally{if(browser)await browser.close().catch(()=>{});}
})();
