const {chromium}=require(process.env.PW_PACKAGE_PATH);
// 主题菜单窄验：真实 iframe 内 selbar 与 npop 在 light/paper/dark 下的 computed 背景/文字；
// shade 色点 hover；主题切换不重建 iframe。全部以 computed style 判定（不看 CSS 变量）。
const WEB=`http://127.0.0.1:${process.argv[2]}`;
const PID=process.argv[3];
const RESULT=process.argv[4];
const R=[];
const ok=(n,p,d)=>R.push({name:n,pass:!!p,detail:d===undefined?'':d});

const lum=rgb=>{const m=/rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(rgb||'');if(!m)return null;
  const [r,g,b]=[+m[1],+m[2],+m[3]].map(v=>v/255).map(v=>v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4));
  return 0.2126*r+0.7152*g+0.0722*b;};

async function frame(page){
  const h=await page.$('#rdrr-pane-source .rdrr-tfhost iframe');
  return h?h.contentFrame():null;
}
async function setTheme(page,label){
  const open=async()=>(await page.locator('.rdrr-aa').count())>0;
  if(!(await open())){
    await page.locator('.rdrr-tbtn').filter({hasText:'Aa'}).first().click();
    await page.waitForSelector('.rdrr-aa',{timeout:8000}).catch(()=>{});
  }
  const btn=page.locator('.rdrr-aa button').filter({hasText:label}).first();
  if(await btn.count()){await btn.click();await page.waitForTimeout(700);}
  if(await open()){await page.keyboard.press('Escape');await page.waitForTimeout(250);}
  if(await page.locator('.rdrr-aa').count()){await page.mouse.click(8,page.viewportSize().height-8);await page.waitForTimeout(200);}
}
async function selectSomething(page,f){
  // 真实鼠标拖选：iframe 内取一个可见 occurrence 的字形矩形 → page 坐标拖拽
  const rect=await f.evaluate(()=>{
    const doc=document.getElementById('doc');
    const u=doc.querySelector('.u');
    if(!u)return null;
    const walker=document.createTreeWalker(u,NodeFilter.SHOW_TEXT);
    let n;let best=null;
    while(n=walker.nextNode()){
      const t=(n.textContent||'').trim();if(t.length<6)continue;
      const r=document.createRange();r.setStart(n,0);r.setEnd(n,Math.min(12,t.length));
      const rects=r.getClientRects();
      if(rects.length&&rects[0].width>10){const b=r.getBoundingClientRect();
        best={x:Math.round(b.x),y:Math.round(b.y),w:Math.round(b.width),h:Math.round(b.height)};break;}
    }
    return best;
  });
  if(!rect)return false;
  const box=await page.evaluate(()=>{const el=document.querySelector('#rdrr-pane-source .rdrr-tfhost iframe');
    const r=el.getBoundingClientRect();return {x:r.x,y:r.y};});
  const y=box.y+rect.y+Math.round(rect.h/2);
  await page.mouse.move(box.x+rect.x+1,y);
  await page.mouse.down();
  for(let i=1;i<=8;i++)await page.mouse.move(box.x+rect.x+1+(rect.w-2)*i/8,y);
  await page.mouse.up();
  await page.waitForTimeout(700);
  return true;
}

(async()=>{
 const browser=await chromium.launch({headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:940}});
 page.setDefaultTimeout(20000);
 try{
  await page.goto(`${WEB}/#/p/${PID}/reader`,{waitUntil:'domcontentloaded'});
  await page.waitForSelector('#btn-aifold',{timeout:25000});
  const stage=await page.locator('#btn-aifold').getAttribute('data-stage');
  if(stage!=='on'){await page.locator('#btn-aifold').click();
    try{await page.waitForSelector('#lvlbar',{timeout:15000});}
    catch(e){if(await page.locator('#btn-aifold[data-stage="ready"]').count()){
      await page.locator('#btn-aifold').click();await page.waitForSelector('#lvlbar',{timeout:15000});}}}
  await page.waitForTimeout(1600);
  let f=await frame(page);
  let curFrame=f;
  ok('源栏内核 iframe 就绪',!!f);
  if(!f){throw new Error('no frame');}
  // 标记 iframe 实例，主题切换后必须仍在（不重建）
  await f.evaluate(()=>{window.__themeMark='mark-'+Math.random().toString(36).slice(2);});
  const mark0=await f.evaluate(()=>window.__themeMark);

  const themes=[
    {label:'浅色', key:'light', theme:'light', style:''},
    {label:'纸色', key:'paper', theme:'light', style:'paper'},
    {label:'深色', key:'dark',  theme:'dark',  style:''},
  ];
  for(const t of themes){
    f=curFrame&&!curFrame.isDetached()?curFrame:(await frame(page));
    if(!f){ok(`[${t.key}] 内核 iframe 可用`,false,'detached');break;}
    await setTheme(page,t.label);
    const htmlState=await f.evaluate(()=>({theme:document.documentElement.dataset.theme||'',
      style:document.documentElement.dataset.style||''}));
    ok(`[${t.key}] 内核主题属性正确`,htmlState.theme===t.theme&&htmlState.style===t.style,htmlState);
    const sel=await selectSomething(page,f);
    const bar=await f.evaluate(()=>{const b=document.querySelector('.selbar');
      if(!b||b.hidden)return null;const cs=getComputedStyle(b);
      const btn=b.querySelector('button[data-act="hl"]');
      return {bg:cs.backgroundColor,color:cs.color,border:cs.borderColor,
        btnColor:btn?getComputedStyle(btn).color:null,visible:b.getBoundingClientRect().width>10};});
    const barLum=lum(bar&&bar.bg), txtLum=lum(bar&&bar.color);
    ok(`[${t.key}] selbar 可见且 computed 配色非黑底黑字`,
       !!bar&&bar.visible&&barLum!==null&&txtLum!==null&&Math.abs(barLum-txtLum)>0.25,
       {selBarFound:sel,bar});
    if(t.key==='light')ok('[light] selbar 浅底深字',barLum>0.5&&txtLum<0.4,{barLum,txtLum});
    if(t.key==='paper')ok('[paper] selbar 暖纸底深字',barLum>0.5&&txtLum<0.4,{barLum,txtLum,bg:bar&&bar.bg});
    if(t.key==='dark')ok('[dark] selbar 深底浅字',barLum<0.35&&txtLum>0.5,{barLum,txtLum});
    // shade 色点 hover：背景/描边应随 hover 变化（颜色保留）
    // 真实指针 hover（CSS :hover 只认真实指针）
    const swBox=await f.evaluate(()=>{const sw=document.querySelector('.selbar .sw');
      if(!sw)return null;const r=sw.getBoundingClientRect();return {x:Math.round(r.x),y:Math.round(r.y),
        w:Math.round(r.width),h:Math.round(r.height)};});
    let shade=null;
    if(swBox){
      const beforeHover=await f.evaluate(()=>{const sw=document.querySelector('.selbar .sw');
        const cs=getComputedStyle(sw);return {bg:cs.backgroundColor,shadow:cs.boxShadow,color:cs.color,
          transform:cs.transform,outline:cs.outlineWidth+' '+cs.outlineColor};});
      const iframeBox=await page.evaluate(()=>{const el=document.querySelector('#rdrr-pane-source .rdrr-tfhost iframe');
        const r=el.getBoundingClientRect();return {x:r.x,y:r.y};});
      await page.mouse.move(iframeBox.x+swBox.x+Math.round(swBox.w/2),iframeBox.y+swBox.y+Math.round(swBox.h/2));
      await page.waitForTimeout(350);
      const afterHover=await f.evaluate(()=>{const sw=document.querySelector('.selbar .sw');
        const cs=getComputedStyle(sw);return {bg:cs.backgroundColor,shadow:cs.boxShadow,color:cs.color,
          transform:cs.transform,outline:cs.outlineWidth+' '+cs.outlineColor};});
      shade={b:beforeHover,after:afterHover};
    }
    // 上游 hover 反馈 = transform: scale(1.18)，颜色刻意不变（“色点颜色保留”）
    const scaleChanged=!!shade&&shade.b.transform!==shade.after.transform;
    const colorKept=!!shade&&shade.b.bg===shade.after.bg
      &&shade.b.shadow===shade.after.shadow&&shade.b.color===shade.after.color;
    const colored=!!shade&&!/^rgba\(0, 0, 0, 0\)$/.test(shade.b.bg)&&shade.b.bg!=='transparent';
    ok(`[${t.key}] shade 色点 hover：有可见反馈（transform）且颜色保留不变`,
       scaleChanged&&colorKept&&colored,shade);
    // npop：点 selbar 的「笔记」按钮打开笔记浮窗
    const npop=await f.evaluate(async()=>{
      const b=document.querySelector('.selbar');
      if(!b||b.hidden)return null;
      const noteBtn=b.querySelector('button[data-act="note"]');
      if(!noteBtn)return {err:'no note btn'};
      noteBtn.click();
      await new Promise(r=>setTimeout(r,400));
      const pop=document.querySelector('.npop, .notepop, .note-pop');
      if(!pop)return {err:'no npop element'};
      const cs=getComputedStyle(pop);
      return {bg:cs.backgroundColor,color:cs.color};
    });
    const nLum=lum(npop&&npop.bg), nTxt=lum(npop&&npop.color);
    ok(`[${t.key}] npop 可见且 computed 配色可读`,
       !!npop&&!npop.err&&nLum!==null&&nTxt!==null&&Math.abs(nLum-nTxt)>0.25,{npop,nLum,nTxt});
    await page.keyboard.press('Escape');await page.keyboard.press('Escape');await page.waitForTimeout(300);
    curFrame=await frame(page);
  }
  // 独立阶段：仅切换主题（不做其他交互），验证 iframe 实例不被重建
  f=await frame(page);
  if(f){
    await f.evaluate(()=>{window.__themeMark2='m2-'+Math.random().toString(36).slice(2);});
    const before=await f.evaluate(()=>window.__themeMark2);
    await setTheme(page,'浅色');await setTheme(page,'纸色');await setTheme(page,'深色');await setTheme(page,'浅色');
    const sameFrame=await frame(page);
    const after=sameFrame?await sameFrame.evaluate(()=>window.__themeMark2).catch(()=>null):null;
    ok('纯主题切换不重建内核 iframe（实例标记保持）',!!after&&after===before,{before,after});
  }
  await page.screenshot({path:'tmp/reader-theme-menu-final.png'});
 }catch(e){ok('探针执行完成',false,String(e).slice(0,300));}
 finally{await browser.close();}
 const failed=R.filter(r=>!r.pass);
 require('fs').writeFileSync(RESULT,JSON.stringify({assertions:R,results:R,failed:failed.length},null,1));
 console.log(JSON.stringify({failed:failed.length,total:R.length,results:R},null,1).slice(0,4000));
 process.exit(failed.length?1:0);
})().catch(e=>{console.error('FATAL',String(e).slice(0,200));process.exit(9);});
