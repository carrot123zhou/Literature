const {chromium}=require(process.env.PW_PACKAGE_PATH);

// AIFold 进度详情（run.activity）前端验收。
// argv: [web, api, pid, resultPath]
//
// 契约边界：fold-run 的状态由本脚本**路由注入**（脚本化快照），因此这里验证的是
// 「前端如何呈现后端 activity」——阶段/当前节/批次/实时计时/待处理/失败/重试、
// 缺失 activity 时的诚实降级、暂停/继续/失败的可用性，以及「只有一个 AIFold 按钮 + 进度环」。
// 真实后端是否真的写入这些字段不在本探针范围内（另由后端测试与真机验收覆盖）。
const WEB=`http://127.0.0.1:${process.argv[2]}`;
const API=`http://127.0.0.1:${process.argv[3]}`;
const PID=process.argv[4];
const RESULT=process.argv[5];

const checks=[];
const pageerrors=[];
const ok=(name,pass,detail)=>{checks.push({name:String(name),pass:!!pass,detail:detail===undefined?'':detail});return !!pass;};

/** 路由注入的 fold-run 快照：脚本随时改写，轮询每次都读到当前值。 */
const FOLD={script:null};

const isoAgo=(ms)=>new Date(Date.now()-ms).toISOString();

function snapshot({status='running',ratio=0.25,activity,errors=[],failed=0,total=8}={}){
  FOLD.script={
    run_id:'fr-activity-check', locale:'zh-CN', status, ready:false,
    progress:{total, completed:0, reused:0, failed, pending:0, ratio, current_section:''},
    errors, activity,
  };
  return FOLD.script;
}

async function installRoutes(page){
  await page.route(/\/reader\/fold-runs$/,async route=>{
    if(route.request().method()!=='POST')return route.continue();
    return route.fulfill({status:200,contentType:'application/json',
      body:JSON.stringify({run_id:'fr-activity-check',created:true,locale:'zh-CN',
        run:{run_id:'fr-activity-check',status:'running'}})});
  });
  await page.route(/\/reader\/fold-runs\/[^/]+\/(pause|resume)$/,async route=>{
    const pause=route.request().url().endsWith('/pause');
    if(FOLD.script){
      FOLD.script={...FOLD.script,status:pause?'paused':'running'};
    }
    return route.fulfill({status:200,contentType:'application/json',
      body:JSON.stringify({run_id:'fr-activity-check',accepted:true})});
  });
  await page.route(/\/reader\/fold-run$/,async route=>{
    const body=FOLD.script||{run_id:'',status:'idle',progress:{ratio:0,total:0,completed:0,reused:0,failed:0,pending:0},errors:[]};
    return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
  });
  // 产物接口受控失败：只验证 ready → 启动 → 载入失败的降级路径（不提供伪造 paper-data）。
  await page.route(/\/reader\/layers\/native/,async route=>route.fulfill({
    status:500,contentType:'application/json',
    body:JSON.stringify({detail:'受控：折叠产物不可用'})}));
}

const chip=page=>page.locator('#rdrr-foldact');
async function chipText(page){
  if(!(await chip(page).count()))return '';
  return (await chip(page).innerText()).replace(/\s+/g,' ').trim();
}
/** 从「已 1:05」/「等 0:12」文本里取秒数；没有计时片段返回 null。 */
function clockSeconds(text){
  const m=/([已等])\s+(\d+):(\d{2})/.exec(text||'');
  return m?Number(m[2])*60+Number(m[3]):null;
}
async function waitFor(fn,timeout=8000,interval=120){
  const deadline=Date.now()+timeout;
  let last;
  while(Date.now()<deadline){
    last=await fn();
    if(last)return last;
    await new Promise(r=>setTimeout(r,interval));
  }
  return last;
}
const stage=page=>page.locator('#btn-aifold').getAttribute('data-stage');
const aifoldLabel=page=>page.locator('#btn-aifold').evaluate(el=>el.innerText.replace(/\s+/g,' ').trim());

async function openReader(page){
  await page.goto(`${WEB}/#/p/${PID}/reader`,{waitUntil:'domcontentloaded'});
  await waitForShell(page);
}
/** 重新加载同一地址：只在 hash 不变的 goto 不会真的重新加载，用 reload 才是新会话。 */
async function reloadReader(page){
  await page.reload({waitUntil:'domcontentloaded'});
  await waitForShell(page);
}
async function waitForShell(page){
  await page.waitForSelector('#btn-aifold',{timeout:20000});
  await page.waitForSelector('.rdrr-pane-body',{timeout:20000});
}

async function main(){
  const browser=await chromium.launch();
  try{
    /* ---------- 桌面宽屏：活动行内容 / 实时计时 / 暂停继续 / 失败 ---------- */
    {
      const page=await browser.newPage({viewport:{width:1360,height:900}});
      page.on('pageerror',e=>pageerrors.push(String(e).slice(0,300)));
      await installRoutes(page);
      await openReader(page);

      ok('初始为普通阅读：单一 AIFold 按钮、无活动行、无进度环',
        (await stage(page))==='off' && await chip(page).count()===0
        && await page.locator('.rdrr-ring').count()===0
        && await page.locator('#btn-foldpause').count()===0,
        `stage=${await stage(page)}`);

      // 生成中（真实流式形态）：delta 每块都把 last_response_at 推到 t0 之后，
      // 计时必须仍按请求起算显示「已 request elapsed」，不能重置成「等 0」。
      const startedAt=isoAgo(65000);
      snapshot({ratio:0.38,activity:{stage:'generating',section_id:'S12',batch_index:2,batch_total:5,
        request_started_at:startedAt,last_response_at:isoAgo(900),elapsed_seconds:65,pending_units:4}});
      await page.locator('#btn-aifold').click();
      await waitFor(async()=>(await stage(page))==='processing'&&/生成中/.test(await chipText(page)));

      const text=await chipText(page);
      const streamSecs=clockSeconds(text);
      ok('活动行显示阶段/当前节/批次/本节待补', /生成中/.test(text)&&/S12/.test(text)&&/批 3\/5/.test(text)&&/本节待补 4/.test(text), text);
      ok('流式 delta 更新（last_response_at 晚于 request_started_at）时仍显示「已」请求耗时',
        /已 \d+:\d{2}/.test(text)&&streamSecs!==null&&streamSecs>=60, text);
      ok('活动行不是横幅：位于顶栏且高度紧凑',
        await chip(page).evaluate(el=>Boolean(el.closest('.rdrr-topbar'))&&el.getBoundingClientRect().height<26),
        String(await chip(page).evaluate(el=>el.getBoundingClientRect().height)));
      ok('进度环仍在（ratio 0.38）',
        /0\.38/.test(await page.locator('.rdrr-ring').getAttribute('style')||''),
        await page.locator('.rdrr-ring').getAttribute('style'));
      ok('处理中同一按钮显示「暂停」', /暂停/.test(await aifoldLabel(page)), await aifoldLabel(page));

      // 计时必须自己走：轮询返回的快照不变（同一时间戳），秒数仍要增长。
      const t1=clockSeconds(await chipText(page));
      await new Promise(r=>setTimeout(r,2600));
      const t2=clockSeconds(await chipText(page));
      ok('等待/生成期间计时逐秒推进（不依赖轮询变化）', t1!==null&&t2!==null&&t2>=t1+2, `t1=${t1} t2=${t2}`);

      // 等待响应：改用 last_response_at 计时，前缀「等」。
      snapshot({ratio:0.5,activity:{stage:'waiting',section_id:'S13',request_started_at:null,
        last_response_at:isoAgo(4000),pending_units:3}});
      await waitFor(async()=>/等待响应/.test(await chipText(page)),10000);
      const waitText=await chipText(page);
      ok('等待响应阶段如实显示并计时', /等待响应/.test(waitText)&&/等 \d+:\d{2}/.test(waitText)&&/本节待补 3/.test(waitText), waitText);
      const w1=clockSeconds(waitText);
      await new Promise(r=>setTimeout(r,2400));
      const w2=clockSeconds(await chipText(page));
      ok('等待期间计时继续推进', w1!==null&&w2!==null&&w2>=w1+2, `w1=${w1} w2=${w2}`);

      // waiting 也是流式阶段：只要拿得到 request_started_at，就按请求起算（不因 delta 重置）。
      snapshot({ratio:0.52,activity:{stage:'waiting',section_id:'S13',
        request_started_at:isoAgo(8000),last_response_at:isoAgo(700),pending_units:3}});
      // 上一个 case 也是「等待响应」：必须等新快照被轮询到（出现「已」前缀）再取基线。
      await waitFor(async()=>/已 \d+:\d{2}/.test(await chipText(page)));
      const waitInFlight=await chipText(page);
      const wi=clockSeconds(waitInFlight);
      const wiPrefix=/已 \d+:\d{2}/.test(waitInFlight);
      await new Promise(r=>setTimeout(r,2400));
      const wi2=clockSeconds(await chipText(page));
      ok('waiting 有 request_started_at 时显示「已」且继续推进（不重置为 0）',
        wiPrefix&&wi!==null&&wi>=7&&wi2!==null&&wi2>=wi+2, `${waitInFlight} → ${await chipText(page)}`);

      // 暂停（状态由后端给出）：时间停表，不显示会递增的假计时。
      snapshot({status:'paused',ratio:0.5,activity:{stage:'generating',section_id:'S13',request_started_at:isoAgo(9000)}});
      await waitFor(async()=>/已暂停/.test(await chipText(page)),10000);
      const pausedText=await chipText(page);
      await new Promise(r=>setTimeout(r,2200));
      const pausedLater=await chipText(page);
      ok('暂停后显示「已暂停」且不再递增计时',
        /已暂停/.test(pausedText)&&clockSeconds(pausedText)===null&&pausedLater===pausedText,
        `${pausedText} → ${pausedLater}`);
      ok('暂停后同一按钮变「继续」且标记 data-paused',
        /继续/.test(await aifoldLabel(page))
        && (await page.locator('#btn-aifold').getAttribute('data-paused'))==='1'
        && await page.locator('#btn-foldpause').count()===0,
        await aifoldLabel(page));

      // 继续：回到处理中并重新计时。
      snapshot({ratio:0.55,activity:{stage:'derive',section_id:'S13',request_started_at:isoAgo(1000),pending_units:2}});
      await page.locator('#btn-aifold').click();
      await waitFor(async()=>/派生译文/.test(await chipText(page)));
      const resumeText=await chipText(page);
      ok('继续后回到处理中并恢复计时',
        /派生译文/.test(resumeText)&&/已 \d+:\d{2}/.test(resumeText)
        && (await page.locator('#btn-aifold').getAttribute('data-paused'))==='0'
        && /暂停/.test(await aifoldLabel(page)) && await page.locator('.rdrr-ring').count()===1,
        resumeText);

      // 部分失败但仍在跑：失败数可见，细节进 tooltip。
      snapshot({ratio:0.62,failed:2,errors:[],activity:{stage:'generating',section_id:'S14',request_started_at:isoAgo(2000),pending_units:9}});
      await waitFor(async()=>/失败 2/.test(await chipText(page)));
      const failedPart=page.locator('#rdrr-foldact [data-part="failed"]');
      ok('失败数在活动行可见且带原因提示',
        /失败 2/.test(await chipText(page)) && Boolean((await failedPart.getAttribute('title')||'').length),
        await failedPart.getAttribute('title'));

      // 契约兼容：后端内部阶段名 + provider 的 state 字段 + epoch 秒时间戳都要正确呈现。
      snapshot({ratio:0.7,activity:{stage:'source_fold',section_id:'S7',
        request_started_at:Date.now()/1000-30}});
      await waitFor(async()=>/折叠原文/.test(await chipText(page)));
      const epochText=await chipText(page);
      ok('内部阶段名有中文标签，epoch 秒时间戳能算出已用时间',
        /折叠原文/.test(epochText)&&clockSeconds(epochText)!==null&&clockSeconds(epochText)>=29
        && clockSeconds(epochText)<=45, epochText);

      snapshot({ratio:0.72,activity:{state:'generating',section_id:'S7',section:undefined,
        request_started_at:Date.now()/1000-3}});
      await waitFor(async()=>/生成中/.test(await chipText(page)));
      ok('只有 provider 的 state 字段时也能识别为生成中', /生成中/.test(await chipText(page)), await chipText(page));

      // 真实后端现形（读完 emit 并受控跑一次 generate 得到）：request_started_at 略早于 last_response_at
      // = 上一个请求已完成，不是在途；此时必须显示「等 X」而不是把上一请求起点当成本次耗时。
      snapshot({ratio:0.73,activity:{stage:'validating',section_id:'S9',
        request_started_at:isoAgo(9000),last_response_at:isoAgo(300),elapsed_seconds:8.7,pending_units:3}});
      await waitFor(async()=>/校验中/.test(await chipText(page)));
      const doneText=await chipText(page);
      const doneSecs=clockSeconds(doneText);
      ok('已完成请求的形态显示为「等」（不把上一请求起点当本次耗时）',
        /等 \d+:\d{2}/.test(doneText)&&doneSecs!==null&&doneSecs<=10, doneText);

      // 公开阶段值 apparatus（书目/索引识别书后材料）与 0 起 batch_index（index=0 → 第 1 批）。
      snapshot({ratio:0.735,activity:{stage:'apparatus',section_id:'S9',batch_index:0,batch_total:3,
        pending_units:0,request_started_at:isoAgo(2000)}});
      await waitFor(async()=>/识别书后材料/.test(await chipText(page)));
      const appText=await chipText(page);
      ok('apparatus 阶段有中文标签，batch_index=0 呈现为第 1 批，待补 0 如实显示',
        /识别书后材料/.test(appText)&&/批 1\/3/.test(appText)&&/本节待补 0/.test(appText), appText);

      snapshot({ratio:0.74,activity:{stage:'generating',section_id:'S7',request_started_at:Date.now()/1000-4,
        retry_reason:'超时后已切句窗重发'}});
      await waitFor(async()=>/曾重试/.test(await chipText(page)));
      const retryPart=page.locator('#rdrr-foldact [data-part="retry"]');
      ok('重试以「曾重试」+原因呈现（不冒充当前在重试）',
        /曾重试：超时后已切句窗重发/.test(await chipText(page))
        && /不代表当前请求在重试/.test((await retryPart.getAttribute('title'))||''),
        await retryPart.getAttribute('title'));

      // 整体失败：活动行消失，错误仍可见（可用），单一按钮回到 AIFold。
      snapshot({status:'failed',ratio:0.93,activity:null,
        errors:[{section_id:'S85',error:'模型 JSON 格式错误',at:isoAgo(1000)}]});
      await waitFor(async()=>(await stage(page))==='off',10000);
      const alert=page.locator('.rdrr-layerfail[role="alert"]');
      ok('失败后活动行清除、错误信息仍可见',
        await chip(page).count()===0 && await alert.count()>0
        && /模型 JSON 格式错误/.test(await alert.first().innerText()),
        (await alert.first().innerText().catch(()=>'')));

      // 缺失 activity（旧后端/旧 run）：只显示状态文本，不编造阶段，不崩。
      snapshot({ratio:0.5,activity:null});
      await page.locator('#btn-aifold').click();
      await waitFor(async()=>(await stage(page))==='processing');
      await new Promise(r=>setTimeout(r,1600));
      ok('后端未提供 activity 时诚实降级：无活动行、仍可暂停、进度环照常',
        await chip(page).count()===0 && /暂停/.test(await aifoldLabel(page))
        && await page.locator('.rdrr-ring').count()===1,
        `stage=${await stage(page)} chip=${await chipText(page)}`);

      // 空闲（无运行作业）：重开页面后没有任何常驻横幅。
      snapshot({status:'idle',ratio:0,activity:null});
      await reloadReader(page);
      await new Promise(r=>setTimeout(r,1200));
      ok('无运行作业时页面无活动行/无告警',
        (await stage(page))==='off' && await chip(page).count()===0
        && await page.locator('.rdrr-layerfail').count()===0,
        `stage=${await stage(page)} chip=${await chipText(page)}`);

      // 就绪：活动行必须退场（不是常驻信息），按钮回到「开始折叠」；启动失败要能回到 ready。
      snapshot({status:'ready',ratio:1});
      FOLD.script.ready=true;
      await reloadReader(page);
      await waitFor(async()=>(await stage(page))==='ready',10000);
      ok('就绪后活动行退场、按钮为「开始折叠」且进度环消失',
        await chip(page).count()===0&&/开始折叠/.test(await aifoldLabel(page))
        && await page.locator('.rdrr-ring').count()===0,
        `${await aifoldLabel(page)}`);
      await page.locator('#btn-aifold').click();
      await waitFor(async()=>(await stage(page))==='ready'
        && /受控：折叠产物不可用/.test(await page.locator('.rdrr-layerfail').first().innerText().catch(()=>'')));
      ok('折叠产物载入失败时如实报错并退回 ready（不假装进入折叠）',
        (await stage(page))==='ready'&&/开始折叠/.test(await aifoldLabel(page))
        && await chip(page).count()===0,
        await page.locator('.rdrr-layerfail').first().innerText().catch(()=>''));

      ok('全程无未捕获页面错误', pageerrors.length===0, pageerrors.join(' | '));
      await page.close();
    }

    /* ---------- 窄屏：活动摘要接到已有副标题，不挤顶栏、不横向溢出 ---------- */
    {
      const page=await browser.newPage({viewport:{width:390,height:844}});
      page.on('pageerror',e=>pageerrors.push(String(e).slice(0,300)));
      await installRoutes(page);
      snapshot({ratio:0.3,activity:{stage:'generating',section_id:'S12',batch_index:1,batch_total:4,
        request_started_at:isoAgo(12000),pending_units:5}});
      await openReader(page);
      // 快照已为 running：页面加载即接续到处理中，不需要（也不应该）再点按钮——那一下是暂停。
      await waitFor(async()=>(await stage(page))==='processing'
        && /生成中/.test(await page.locator('.rdrr-book-sub').innerText().catch(()=>'')));
      const sub=(await page.locator('.rdrr-book-sub').innerText()).replace(/\s+/g,' ').trim();
      ok('窄屏活动摘要显示在副标题（阶段/批次/本节待补）',
        /生成中/.test(sub)&&/批 2\/4/.test(sub)&&/本节待补 5/.test(sub), sub);
      ok('窄屏不渲染单独活动行', await chip(page).count()===0, `chip=${await chipText(page)}`);
      const overflow=await page.evaluate(()=>({
        doc:document.documentElement.scrollWidth-window.innerWidth,
        bar:(()=>{const el=document.querySelector('.rdrr-topbar');return el?el.scrollWidth-el.clientWidth:0;})(),
      }));
      ok('窄屏无横向溢出（页面与顶栏）', overflow.doc<=2&&overflow.bar<=2, JSON.stringify(overflow));
      ok('窄屏仍只有一个 AIFold 按钮', await page.locator('#btn-aifold').count()===1
        && await page.locator('#btn-foldpause').count()===0);

      // 中等宽度（900–1180）：活动行仍在顶栏，不能挤出横向滚动。
      await page.setViewportSize({width:960,height:800});
      await waitFor(async()=>await chip(page).count()>0);
      const midOverflow=await page.evaluate(()=>({doc:document.documentElement.scrollWidth-window.innerWidth,
        bar:(()=>{const el=document.querySelector('.rdrr-topbar');return el?el.scrollWidth-el.clientWidth:0;})()}));
      ok('中等宽度显示紧凑活动行且不溢出', await chip(page).count()>0&&midOverflow.doc<=2&&midOverflow.bar<=2,
        `${await chipText(page)} ${JSON.stringify(midOverflow)}`);

      // 极窄（320px）：约束下限，不横向滚动、活动摘要仍可见。
      await page.setViewportSize({width:320,height:800});
      await new Promise(r=>setTimeout(r,300));
      const tiny=await page.evaluate(()=>({doc:document.documentElement.scrollWidth-window.innerWidth,
        bar:(()=>{const el=document.querySelector('.rdrr-topbar');return el?el.scrollWidth-el.clientWidth:0;})()}));
      const tinySub=(await page.locator('.rdrr-book-sub').innerText().catch(()=>'')).replace(/\s+/g,' ').trim();
      ok('320px 不横向溢出且活动摘要仍可读', tiny.doc<=2&&tiny.bar<=2&&/生成中/.test(tinySub),
        `${JSON.stringify(tiny)} sub=${tinySub}`);
      await page.close();
    }
  }finally{
    await browser.close();
  }

  const payload={checks,pageerrors};
  require('fs').writeFileSync(RESULT,JSON.stringify(payload,null,2));
  console.log(JSON.stringify(payload));
  process.exit(checks.filter(c=>!c.pass).length===0?0:1);
}

main().catch(error=>{
  console.error(String(error&&error.stack||error));
  try{require('fs').writeFileSync(RESULT,JSON.stringify({checks,pageerrors,error:String(error)},null,2));}catch(e){}
  process.exit(1);
});
